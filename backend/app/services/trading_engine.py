"""Trading Engine — MongoDB/Beanie edition.

Pipeline for one tick of one agent:
  1. Pull candles from the agent's exchange.
  2. Ask the strategy for a signal.
  3. If the signal is enter/exit:
     a. Run RiskEngine.evaluate_entry (entries only).
     b. Place the order via the exchange client.
     c. Persist a Trade doc + (for entries) a Position doc.
     d. Update agent counters and emit a notification.
  4. If no actionable signal, persist nothing.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.models.agent import Agent
from app.models.api_key import ApiKey
from app.models.position import Position
from app.models.trade import Trade
from app.services.exchange import OrderRequest, build_client
from app.services.exchange.base import ExchangeError, OrderResult
import app.services.grok_analyst as grok_analyst
from app.services.notifications import NotificationService
from app.services.risk_engine import RiskEngine
from app.services.strategy import StrategyContext, get_strategy
from app.services.strategy.indicators import candles_to_df


class TradingEngine:
    """One-shot per-agent execution. Designed for the scheduler tick."""

    async def run_agent_tick(self, agent: Agent, api_key: ApiKey) -> dict[str, Any]:
        if agent.status != "active":
            return {"skipped": True, "reason": f"agent {agent.status}"}

        now = datetime.now(timezone.utc)

        # --- Session cooldown: auto-resume when time is up ---
        if agent.cooldown_until:
            if now < agent.cooldown_until:
                remaining = (agent.cooldown_until - now).total_seconds() / 60
                agent.last_error = f"cooldown: studying fleet data ({remaining:.0f}m remaining)"
                agent.last_tick_at = now
                await agent.save()
                return {"skipped": True, "reason": f"cooldown ({remaining:.0f}m left)"}
            else:
                # Cooldown expired — auto-resume, study fleet, reset session
                agent.cooldown_until = None
                agent.session_trade_count = 0
                agent.last_error = None
                await agent.save()
                try:
                    await self._study_fleet_and_adapt(agent)
                except Exception:
                    pass
                logger.info("agent {} auto-resumed after cooldown — session reset, fleet studied", agent.id)
                await NotificationService.create(
                    user_id=agent.user_id,
                    type="agent_status",
                    title=f"{agent.name} resumed trading",
                    message="Cooldown ended. Fleet data studied. New session started.",
                    data={"agent_id": str(agent.id), "trigger": "cooldown_end"},
                )

        strategy_type = agent.strategy.type if agent.strategy else None
        if not strategy_type:
            return {"skipped": True, "reason": "agent has no strategy"}

        strategy = get_strategy(strategy_type)

        # --- Profit protection ---
        capital = float(agent.assigned_capital or 0)
        if capital > 0:
            pnl_pct = (float(agent.total_pnl or 0) / capital) * 100
            protect_threshold = float(agent.profit_protect_pct or 15)
            if pnl_pct >= protect_threshold and not agent.protect_mode:
                agent.protect_mode = True
                logger.info("agent {} entered protect mode: PnL {:.1f}% >= {:.1f}%", agent.id, pnl_pct, protect_threshold)
                await NotificationService.create(
                    user_id=agent.user_id,
                    type="agent_status",
                    title=f"{agent.name} in profit protection mode",
                    message=f"PnL reached {pnl_pct:.1f}% — now only taking high-confidence trades at reduced size.",
                    data={"agent_id": str(agent.id), "trigger": "profit_protect"},
                )
            elif pnl_pct < protect_threshold * 0.5 and agent.protect_mode:
                agent.protect_mode = False

        # --- AI win-rate watchdog: if win rate is poor, force a study break ---
        total_t = agent.total_trades or 0
        if total_t >= 5:
            win_rate = (agent.winning_trades or 0) / total_t
            pnl = float(agent.total_pnl or 0)
            # Poor win rate AND losing money → force cooldown to learn
            if win_rate < 0.35 and pnl < 0 and not agent.cooldown_until:
                from datetime import timedelta
                agent.cooldown_until = now + timedelta(hours=agent.cooldown_hours or 3)
                agent.last_error = f"AI watchdog: win rate {win_rate:.0%} too low with negative PnL — forced study break"
                await agent.save()
                logger.warning(
                    "agent {} AI watchdog triggered: win_rate={:.0%} pnl={:.2f} — forcing {:.0f}h cooldown",
                    agent.id, win_rate, pnl, agent.cooldown_hours or 3,
                )
                await NotificationService.create(
                    user_id=agent.user_id,
                    type="agent_status",
                    title=f"{agent.name} paused by AI watchdog",
                    message=f"Win rate {win_rate:.0%} with ${pnl:.2f} PnL. Pausing to study fleet data and re-optimize.",
                    data={"agent_id": str(agent.id), "trigger": "ai_watchdog"},
                )
                return {"skipped": True, "reason": "AI watchdog forced cooldown"}

        # Record that a tick ran even if it errors out below.
        agent.last_tick_at = now
        agent.tick_count = (agent.tick_count or 0) + 1

        try:
            client = build_client(api_key)
        except PermissionError as exc:
            agent.last_error = str(exc)
            await agent.save()
            logger.warning("agent {}: cannot build client — {}", agent.id, exc)
            return {"skipped": True, "reason": str(exc)}

        signals_summary: list[str] = []
        try:
            for symbol in (agent.trading_pairs or []):
                try:
                    await self._tick_symbol(agent, api_key, strategy, client, symbol)
                    sig = agent.last_signal or "hold"
                    if sig != "hold":
                        signals_summary.append(f"{symbol}:{sig}")
                except Exception as exc:  # noqa: BLE001
                    agent.last_error = f"{symbol}: {exc}"
                    logger.warning("agent {} {}: tick error — {}", agent.id, symbol, exc)
        finally:
            await client.close()

        # Store a summary of non-hold signals so the UI shows what happened
        pairs_checked = len(agent.trading_pairs or [])
        if signals_summary:
            agent.last_error = None
            agent.last_signal_symbol = " | ".join(signals_summary[:3])
        else:
            agent.last_signal = "hold"
            agent.last_signal_symbol = f"scanned {pairs_checked} pairs"
            agent.last_error = None

        await agent.save()
        return {"ok": True}

    # Rough minimum qty per symbol — Bybit rejects orders below these sizes.
    # Keys are prefix-matched (e.g. "BTC" matches BTCUSDT).
    _MIN_QTY: dict[str, float] = {
        "BTC": 0.001,
        "ETH": 0.01,
        "SOL": 0.1,
        "BNB": 0.01,
        "XRP": 1.0,
    }
    _DEFAULT_MIN_QTY = 0.01

    @classmethod
    def _min_qty_for(cls, symbol: str) -> float:
        for prefix, min_q in cls._MIN_QTY.items():
            if symbol.upper().startswith(prefix):
                return min_q
        return cls._DEFAULT_MIN_QTY

    # Symbols that persistently fail on all fallback providers — downgraded to
    # debug so they don't pollute logs on every tick.
    _KNOWN_UNAVAILABLE: frozenset[str] = frozenset({"TONUSDT"})

    async def _tick_symbol(self, agent: Agent, api_key, strategy, client, symbol: str) -> None:
        try:
            raw = await client.get_candles(symbol, agent.timeframe, limit=200)
        except ExchangeError as exc:
            agent.last_error = f"{symbol}: market data unavailable — {exc}"
            if symbol in self._KNOWN_UNAVAILABLE:
                logger.debug("agent {} {}: skipped (no data source): {}", agent.id, symbol, exc)
            else:
                logger.warning("agent {} {}: candle fetch failed: {}", agent.id, symbol, exc)
            return
        if len(raw) < 50:
            return

        df = candles_to_df(raw)

        open_position = await self._open_position(agent.id, symbol)
        ctx = StrategyContext(
            symbol=symbol,
            timeframe=agent.timeframe,
            in_position=open_position is not None,
            position_side=open_position.side if open_position else None,
        )
        # Step 1: Strategy SCREENER — identifies potential opportunities
        signal = strategy.evaluate(df, agent.strategy_params or {}, ctx)

        # Step 2: AI ANALYST — the real brain. Performs deep market analysis
        # on any non-hold signal before allowing entry. The strategy is just
        # a screener; the AI decides whether to actually trade.
        if signal.action != "hold":
            win_rate = (
                (agent.winning_trades / agent.total_trades)
                if agent.total_trades > 0 else 0.5
            )
            signal = await grok_analyst.analyse_market(
                signal,
                df,
                symbol=symbol,
                timeframe=agent.timeframe,
                strategy_type=strategy.type,
                strategy_params=agent.strategy_params or {},
                agent_context={
                    "agent_id": str(agent.id),
                    "agent_name": agent.name,
                    "total_trades": agent.total_trades,
                    "winning_trades": agent.winning_trades,
                    "win_rate": f"{win_rate:.0%}",
                    "total_pnl": float(agent.total_pnl or 0),
                    "current_day_pnl": float(agent.current_day_pnl or 0),
                    "confidence_score": float(agent.confidence_score or 50),
                    "in_position": ctx.in_position,
                    "is_protect_mode": getattr(agent, "protect_mode", False),
                    "session_trades": getattr(agent, "session_trade_count", 0),
                },
            )

        # Track what this tick produced — caller will save the agent.
        agent.last_signal = signal.action
        agent.last_signal_symbol = symbol
        agent.last_error = None

        last_price = float(df["close"].iloc[-1])
        logger.debug(
            "agent {} {} signal={} price={:.4f} confidence={:.2f}",
            agent.id, symbol, signal.action, last_price, signal.confidence,
        )

        if signal.action == "hold":
            return

        if signal.action == "exit" and open_position is not None:
            await self._close_position(agent, api_key, client, open_position, last_price, signal.reason)
            return

        if signal.action in ("enter_long", "enter_short") and open_position is None:
            # In protect mode, only accept very high confidence trades
            min_conf = 0.80 if agent.protect_mode else 0.55
            if signal.confidence < min_conf:
                mode_label = "protect mode" if agent.protect_mode else "standard"
                agent.last_error = f"signal rejected ({mode_label}): confidence {signal.confidence:.2f} < {min_conf}"
                logger.debug(
                    "agent {} {}: entry rejected (confidence {:.2f} < {:.2f}, {})",
                    agent.id, symbol, signal.confidence, min_conf,
                    "PROTECT" if agent.protect_mode else "normal",
                )
                return

            side: str = "long" if signal.action == "enter_long" else "short"
            sl_pct = signal.suggested_stop_loss_pct or 1.0
            tp_pct = signal.suggested_take_profit_pct or 2.5

            decision = await RiskEngine.evaluate_entry(
                agent,
                entry_price=last_price,
                stop_loss_pct=sl_pct,
                side=side,  # type: ignore[arg-type]
            )
            if not decision.approved:
                agent.last_error = f"risk blocked: {decision.reason}"
                await RiskEngine.log_risk_event(
                    user_id=agent.user_id,
                    agent_id=agent.id,
                    event_type=decision.code if decision.code != "ok" else "api_error",
                    severity="warning",
                    message=decision.reason,
                    details={"symbol": symbol, "signal": signal.__dict__},
                )
                return

            # Enforce exchange minimum order size — prevent wasted API calls.
            min_qty = self._min_qty_for(symbol)
            qty = decision.sized_quantity
            if qty < min_qty:
                msg = (
                    f"position size {qty:.6f} below exchange minimum {min_qty} for {symbol}. "
                    f"Increase assigned capital (current: ${agent.assigned_capital:.0f})"
                )
                agent.last_error = msg
                logger.warning("agent {} {}: {}", agent.id, symbol, msg)
                await RiskEngine.log_risk_event(
                    user_id=agent.user_id,
                    agent_id=agent.id,
                    event_type="min_qty",
                    severity="warning",
                    message=msg,
                    details={"symbol": symbol, "qty": qty, "min_qty": min_qty},
                )
                return

            await self._open_trade(
                agent=agent,
                api_key=api_key,
                client=client,
                symbol=symbol,
                side=side,  # type: ignore[arg-type]
                entry_price=last_price,
                quantity=qty,
                stop_loss_pct=sl_pct,
                take_profit_pct=tp_pct,
                signal=signal,
                risk_payload={
                    "approved": True,
                    "reason": decision.reason,
                    "risk_amount": decision.risk_amount,
                    "sl_pct": sl_pct,
                    "tp_pct": tp_pct,
                },
            )

    @staticmethod
    async def _open_position(agent_id, symbol: str) -> Position | None:
        return await Position.find_one(
            Position.agent_id == agent_id,
            Position.symbol == symbol,
            Position.is_open == True,  # noqa: E712
        )

    async def _open_trade(
        self,
        *,
        agent: Agent,
        api_key,
        client,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        signal,
        risk_payload: dict[str, Any],
    ) -> None:
        sl_price = (
            entry_price * (1 - stop_loss_pct / 100)
            if side == "long"
            else entry_price * (1 + stop_loss_pct / 100)
        )
        tp_price = (
            entry_price * (1 + take_profit_pct / 100)
            if side == "long"
            else entry_price * (1 - take_profit_pct / 100)
        )

        order = OrderRequest(
            symbol=symbol,
            side="buy" if side == "long" else "sell",
            order_type="market",
            quantity=quantity,
            stop_loss=sl_price,
            take_profit=tp_price,
            client_order_id=f"agent-{agent.id}-{uuid.uuid4().hex[:8]}",
        )

        if agent.is_paper_trade:
            placed = OrderResult(
                exchange_order_id=f"paper-{uuid.uuid4().hex[:12]}",
                status="filled",
                avg_fill_price=entry_price,
                filled_qty=quantity,
                raw={"paper": True},
            )
        else:
            try:
                placed = await client.place_order(order)
            except ExchangeError as exc:
                agent.last_error = f"order failed: {exc}"
                await RiskEngine.log_risk_event(
                    user_id=agent.user_id,
                    agent_id=agent.id,
                    event_type="api_error",
                    severity="critical",
                    message=f"order placement failed: {exc}",
                    details={"symbol": symbol, "side": side},
                )
                return

        trade = Trade(
            user_id=agent.user_id,
            agent_id=agent.id,
            api_key_id=api_key.id,
            exchange=api_key.exchange,
            exchange_order_id=placed.exchange_order_id,
            symbol=symbol,
            side="buy" if side == "long" else "sell",
            order_type="market",
            status="open",
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=sl_price,
            take_profit=tp_price,
            signal_source=f"strategy:{agent.strategy.type}" if agent.strategy else "strategy",
            signal_data={
                "confidence": signal.confidence,
                "reason": signal.reason,
                **(signal.metadata or {}),
            },
            risk_checks=risk_payload,
            opened_at=datetime.now(timezone.utc),
        )
        await trade.insert()

        position = Position(
            user_id=agent.user_id,
            agent_id=agent.id,
            trade_id=trade.id,
            exchange=api_key.exchange,
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            current_price=entry_price,
            stop_loss=sl_price,
            take_profit=tp_price,
        )
        await position.insert()

        agent.total_trades = (agent.total_trades or 0) + 1
        agent.last_trade_at = datetime.now(timezone.utc)
        agent.confidence_score = max(0, min(100, 50 + (signal.confidence - 0.5) * 100))
        if agent.recovery_mode:
            agent.recovery_mode = False
            logger.info("agent {} recovery trade opened — recovery mode cleared", agent.id)

        # Session trade counter — triggers cooldown after N trades
        agent.session_trade_count = (agent.session_trade_count or 0) + 1
        max_session = agent.trades_per_session or 10
        if agent.session_trade_count >= max_session:
            hours = agent.cooldown_hours or 3.0
            from datetime import timedelta
            agent.cooldown_until = datetime.now(timezone.utc) + timedelta(hours=hours)
            logger.info(
                "agent {} completed session ({} trades) — cooling down for {:.1f}h to study fleet data",
                agent.id, agent.session_trade_count, hours,
            )
            await NotificationService.create(
                user_id=agent.user_id,
                type="agent_status",
                title=f"{agent.name} pausing to study ({agent.session_trade_count} trades done)",
                message=f"Cooling down for {hours:.0f}h to analyse fleet data before next session.",
                data={"agent_id": str(agent.id), "trigger": "session_cooldown"},
            )

        await agent.save()

        await NotificationService.create(
            user_id=agent.user_id,
            type="trade_opened",
            title=f"{agent.name} opened {side} {symbol}",
            message=(
                f"qty {quantity:.6f} @ {entry_price:.4f}, "
                f"SL {sl_price:.4f}, TP {tp_price:.4f}"
            ),
            data={"agent_id": str(agent.id), "trade_id": str(trade.id)},
        )

    async def _close_position(
        self,
        agent: Agent,
        api_key,
        client,
        position: Position,
        last_price: float,
        reason: str,
    ) -> None:
        order = OrderRequest(
            symbol=position.symbol,
            side="sell" if position.side == "long" else "buy",
            order_type="market",
            quantity=float(position.quantity),
            reduce_only=True,
            client_order_id=f"agent-{agent.id}-close-{uuid.uuid4().hex[:8]}",
        )
        if not agent.is_paper_trade:
            try:
                await client.place_order(order)
            except ExchangeError as exc:
                agent.last_error = f"close failed: {exc}"
                await RiskEngine.log_risk_event(
                    user_id=agent.user_id,
                    agent_id=agent.id,
                    event_type="api_error",
                    severity="critical",
                    message=f"position close failed: {exc}",
                    details={"symbol": position.symbol},
                )
                return

        entry_price = float(position.entry_price)
        qty = float(position.quantity)
        gross = (last_price - entry_price) * qty
        if position.side == "short":
            gross = -gross

        position.is_open = False
        position.current_price = last_price
        position.unrealized_pnl = gross
        position.unrealized_pnl_pct = (gross / max(entry_price * qty, 1e-9)) * 100
        await position.save()

        trade = await Trade.get(position.trade_id)
        if trade:
            trade.exit_price = last_price
            trade.pnl = gross
            trade.pnl_pct = (gross / max(entry_price * qty, 1e-9)) * 100
            trade.status = "filled"
            trade.closed_at = datetime.now(timezone.utc)
            trade.notes = reason
            await trade.save()

        agent.total_pnl = float(agent.total_pnl or 0) + gross
        agent.current_day_pnl = float(agent.current_day_pnl or 0) + gross
        agent.current_week_pnl = float(agent.current_week_pnl or 0) + gross
        if gross > 0:
            agent.winning_trades = (agent.winning_trades or 0) + 1
        await agent.save()

        # Feed the outcome back into the fleet learning system so other agents
        # can learn from this trade's success or failure.
        try:
            from app.services.learning import LearningService
            strategy_type = agent.strategy.type if agent.strategy else None
            if strategy_type:
                await LearningService.record_trade_outcome(
                    agent_id=agent.id,
                    strategy_type=strategy_type,
                    symbol=position.symbol,
                    timeframe=agent.timeframe,
                    pnl=gross,
                )
        except Exception:
            pass

        await NotificationService.create(
            user_id=agent.user_id,
            type="trade_closed",
            title=f"{agent.name} closed {position.side} {position.symbol}",
            message=f"PnL {gross:+.2f} ({reason})",
            data={"agent_id": str(agent.id), "trade_id": str(position.trade_id)},
        )

        # Emergency re-optimization: if the agent just hit 3 consecutive losses,
        # enable recovery mode immediately (so it can trade again), then try
        # to re-optimize as best-effort.
        if gross < 0:
            loss_streak = 0
            streak_trades = await Trade.find(
                Trade.agent_id == agent.id, Trade.status == "filled", Trade.closed_at != None,
            ).sort(-Trade.closed_at).limit(5).to_list()
            for t in streak_trades:
                if float(t.pnl or 0) < 0:
                    loss_streak += 1
                else:
                    break

            if loss_streak >= 3 and not agent.recovery_mode:
                # Always enable recovery mode so the agent is unblocked
                agent.recovery_mode = True
                await agent.save()
                logger.warning(
                    "agent {} hit {} consecutive losses — recovery mode ON",
                    agent.id, loss_streak,
                )
                # Try to re-optimize (best-effort — agent trades again regardless)
                try:
                    await self._emergency_optimize(agent, position.symbol, loss_streak)
                except Exception as exc:
                    logger.warning("emergency optimize failed for {}: {} — agent will resume with current params", agent.id, exc)

    async def _emergency_optimize(self, agent: Agent, symbol: str, loss_streak: int) -> None:
        """Re-optimize after consecutive losses. Agent is already in recovery_mode.

        Studies fleet winners, re-optimizes with Bayesian search, and tightens params.
        """
        strategy_type = agent.strategy.type if agent.strategy else None
        if not strategy_type or not agent.api_key_id:
            return

        logger.warning(
            "agent {} — studying fleet winners and re-optimizing after {} losses",
            agent.id, loss_streak,
        )

        from app.models.api_key import ApiKey
        from app.services.exchange import build_client
        from app.services.strategy import get_strategy
        from app.services.strategy.indicators import candles_to_df
        from app.services.ai_optimizer import optimize_strategy_async
        from app.services.learning import LearningService

        api_key = await ApiKey.get(agent.api_key_id)
        if not api_key:
            return

        strat = get_strategy(strategy_type)
        client = build_client(api_key)
        try:
            candles = await client.get_candles(symbol, agent.timeframe, limit=400)
        finally:
            await client.close()

        if len(candles) < 100:
            return

        df = candles_to_df(candles)

        # Study what other agents are doing well — pull fleet-wide winners
        warm_starts = await LearningService.warm_starts(
            strategy_type=strategy_type,
            symbol=symbol,
            timeframe=agent.timeframe,
        )

        # Also look for the best-performing agent on this strategy to learn from
        best_fleet = await LearningService.fleet_best(
            strategy_type=strategy_type, symbol=symbol, limit=3,
        )
        # If fleet has proven winning params with positive realized PnL, prefer those
        fleet_winner_params = None
        for obs in best_fleet:
            if float(obs.realized_pnl or 0) > 0 and int(obs.realized_trades or 0) >= 3:
                fleet_winner_params = dict(obs.params)
                logger.info(
                    "agent {} adopting fleet winner params (realized_pnl={:.2f}, trades={}): {}",
                    agent.id, obs.realized_pnl, obs.realized_trades, obs.params,
                )
                break

        # Start from fleet winner params if available, otherwise from current + defaults
        if fleet_winner_params:
            base = {**(strat.default_params or {}), **fleet_winner_params}
        else:
            base = {**(strat.default_params or {}), **(agent.strategy_params or {})}

        result = await optimize_strategy_async(
            strat, df, base,
            symbol=symbol,
            timeframe=agent.timeframe,
            warm_starts=warm_starts,
            n_calls=15,
        )

        # Apply tighter risk overrides on top of optimized params
        new_params = dict(result.best_params)
        if "stop_loss_pct" in new_params:
            new_params["stop_loss_pct"] = min(new_params["stop_loss_pct"], 0.8)
        if "min_confidence" in new_params:
            new_params["min_confidence"] = max(new_params["min_confidence"], 0.7)
        if "position_size_pct" in new_params:
            new_params["position_size_pct"] = min(new_params["position_size_pct"], 1.5)

        agent.strategy_params = new_params
        agent.optimization_params = {
            "score": result.best_score,
            "trigger": "emergency_loss_streak",
            "loss_streak": loss_streak,
            "iterations": result.iterations,
            "adopted_fleet_winner": fleet_winner_params is not None,
        }
        await agent.save()

        logger.info(
            "agent {} emergency re-optimized (recovery mode ON): score={:.4f} fleet_winner={} params={}",
            agent.id, result.best_score, fleet_winner_params is not None, new_params,
        )

        await NotificationService.create(
            user_id=agent.user_id,
            type="agent_optimized",
            title=f"{agent.name} recovering after {loss_streak} losses",
            message=(
                f"Studied fleet winners, re-optimized params (score: {result.best_score:.3f}). "
                f"Now in recovery mode — next trade at half size to prove new params work."
            ),
            data={"agent_id": str(agent.id), "trigger": "loss_streak", "recovery": True},
        )

    async def _study_fleet_and_adapt(self, agent: Agent) -> None:
        """Called when a session cooldown ends. Study what other agents learned
        and adopt better params if available."""
        strategy_type = agent.strategy.type if agent.strategy else None
        if not strategy_type:
            return

        from app.services.learning import LearningService

        best = await LearningService.fleet_best(
            strategy_type=strategy_type, limit=5,
        )

        # Find the observation with the best realized PnL (not just backtest)
        winner = None
        for obs in best:
            if float(obs.realized_pnl or 0) > 0 and int(obs.realized_trades or 0) >= 3:
                winner = obs
                break

        if winner:
            old_params = dict(agent.strategy_params or {})
            new_params = {**old_params, **winner.params}
            agent.strategy_params = new_params
            await agent.save()
            logger.info(
                "agent {} adopted fleet winner params after cooldown study (realized_pnl={:.2f}): {}",
                agent.id, winner.realized_pnl, winner.params,
            )
            await NotificationService.create(
                user_id=agent.user_id,
                type="agent_optimized",
                title=f"{agent.name} learned from fleet data",
                message=f"Adopted winning params from fleet (realized PnL: ${winner.realized_pnl:.2f}). Resuming trading.",
                data={"agent_id": str(agent.id), "trigger": "cooldown_study"},
            )
        else:
            logger.info("agent {} cooldown study: no fleet winner found, keeping current params", agent.id)
