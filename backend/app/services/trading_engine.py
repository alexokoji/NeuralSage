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

        strategy_type = agent.strategy.type if agent.strategy else None
        if not strategy_type:
            return {"skipped": True, "reason": "agent has no strategy"}

        strategy = get_strategy(strategy_type)

        agent.last_tick_at = now
        agent.tick_count = (agent.tick_count or 0) + 1

        try:
            client = build_client(api_key)
        except PermissionError as exc:
            agent.last_error = str(exc)
            await agent.save()
            return {"skipped": True, "reason": str(exc)}

        # --- Auto-clean: remove crypto pairs from forex agents and vice versa ---
        if self._is_forex(api_key.exchange):
            clean_pairs = [p for p in (agent.trading_pairs or []) if not p.endswith("USDT")]
            if len(clean_pairs) != len(agent.trading_pairs or []):
                agent.trading_pairs = clean_pairs or ["EURUSD"]
                logger.info("agent {} cleaned crypto pairs from forex agent: {}", agent.id, agent.trading_pairs)

        # --- Daily profit protection (resets every day via rollover) ---
        capital = float(agent.assigned_capital or 0)
        if capital > 0:
            day_pnl_pct = (float(agent.current_day_pnl or 0) / capital) * 100
            protect_threshold = float(agent.profit_protect_pct or 15)
            if day_pnl_pct >= protect_threshold and not agent.protect_mode:
                agent.protect_mode = True
                logger.info(
                    "agent {} daily profit protection: today {:.1f}% >= {:.1f}% — protect mode ON",
                    agent.id, day_pnl_pct, protect_threshold,
                )
                await NotificationService.create(
                    user_id=agent.user_id,
                    type="agent_status",
                    title=f"{agent.name} hit daily target ({day_pnl_pct:.1f}% today)",
                    message=f"Protecting today's gains. Higher confidence required for new entries. Resets tomorrow.",
                    data={"agent_id": str(agent.id), "trigger": "daily_profit_protect"},
                )

        # --- Two-phase scan: screener filters, AI analyses top candidates ---
        # Phase 1: Run screener on ALL symbols (free, no API calls)
        candidates: list[tuple[str, Any, Any, Any]] = []  # (symbol, df, screener_signal, open_pos)
        signals_summary: list[str] = []
        try:
            for symbol in (agent.trading_pairs or []):
                try:
                    raw = await client.get_candles(symbol, agent.timeframe, limit=200)
                except ExchangeError as exc:
                    if symbol not in self._KNOWN_UNAVAILABLE:
                        logger.warning("agent {} {}: candle fetch failed: {}", agent.id, symbol, exc)
                    continue
                if len(raw) < 50:
                    continue

                df = candles_to_df(raw)
                open_position = await self._open_position(agent.id, symbol)
                last_price = float(df["close"].iloc[-1])

                # Auto TP/SL for paper trades
                if open_position is not None:
                    open_position.current_price = last_price
                    sl = float(open_position.stop_loss or 0)
                    tp = float(open_position.take_profit or 0)
                    if open_position.side == "long":
                        hit_sl = sl > 0 and last_price <= sl
                        hit_tp = tp > 0 and last_price >= tp
                    else:
                        hit_sl = sl > 0 and last_price >= sl
                        hit_tp = tp > 0 and last_price <= tp
                    if hit_tp:
                        await self._close_position(agent, api_key, client, open_position, tp, "take profit hit")
                        signals_summary.append(f"{symbol}:TP")
                        continue
                    if hit_sl:
                        await self._close_position(agent, api_key, client, open_position, sl, "stop loss hit")
                        signals_summary.append(f"{symbol}:SL")
                        continue

                ctx = StrategyContext(
                    symbol=symbol,
                    timeframe=agent.timeframe,
                    in_position=open_position is not None,
                    position_side=open_position.side if open_position else None,
                )
                screener_signal = strategy.evaluate(df, agent.strategy_params or {}, ctx)

                # Always include: non-hold signals, open positions
                # Conditionally include: holds with high confidence (near threshold)
                if (
                    screener_signal.action != "hold"
                    or open_position is not None
                    or screener_signal.confidence > 0.42
                ):
                    candidates.append((symbol, df, screener_signal, open_position))

            # Phase 2: Groq analyses top 3 candidates per agent
            # Phase 3: GPT makes final decision on Groq's best pick (1 call)
            candidates.sort(key=lambda c: (c[2].action == "hold", -c[2].confidence))

            groq_best = None  # (symbol, df, signal, open_pos)
            for symbol, df, screener_signal, open_position in candidates[:3]:
                try:
                    groq_signal = await grok_analyst.groq_analyse(
                        screener_signal, df,
                        symbol=symbol, timeframe=agent.timeframe,
                        strategy_type=strategy.type,
                        strategy_params=agent.strategy_params or {},
                        agent_context={
                            "agent_name": agent.name,
                            "total_trades": agent.total_trades,
                            "winning_trades": agent.winning_trades,
                            "total_pnl": float(agent.total_pnl or 0),
                            "current_day_pnl": float(agent.current_day_pnl or 0),
                            "in_position": open_position is not None,
                            "screener_said": screener_signal.action,
                            "screener_confidence": screener_signal.confidence,
                        },
                    )
                    if groq_signal.action in ("enter_long", "enter_short", "exit"):
                        if groq_best is None or groq_signal.confidence > groq_best[2].confidence:
                            groq_best = (symbol, df, groq_signal, open_position)
                except Exception as exc:
                    logger.debug("agent {} {} Groq analysis failed: {}", agent.id, symbol, exc)

            # Phase 3: GPT final decision on Groq's best pick
            if groq_best:
                symbol, df, groq_signal, open_position = groq_best
                try:
                    final_signal = await grok_analyst.gpt_decide(
                        groq_signal, df,
                        symbol=symbol, timeframe=agent.timeframe,
                        agent_name=agent.name,
                    )
                    agent.last_signal = final_signal.action
                    agent.last_signal_symbol = symbol
                    await self._execute_signal(agent, api_key, client, symbol, df, final_signal, open_position)
                    if final_signal.action != "hold":
                        signals_summary.append(f"{symbol}:{final_signal.action}")
                except Exception as exc:
                    logger.warning("agent {} GPT decision failed: {} — using Groq signal", agent.id, exc)
                    agent.last_signal = groq_signal.action
                    agent.last_signal_symbol = symbol
                    await self._execute_signal(agent, api_key, client, symbol, df, groq_signal, open_position)
                    if groq_signal.action != "hold":
                        signals_summary.append(f"{symbol}:{groq_signal.action}")

        finally:
            await client.close()

        pairs_checked = len(agent.trading_pairs or [])
        if signals_summary:
            agent.last_error = None
            agent.last_signal_symbol = " | ".join(signals_summary[:3])
        else:
            agent.last_signal = "hold"
            agent.last_signal_symbol = f"scanned {pairs_checked} pairs"
            agent.last_error = None

        # Stuck agent nudge: if no trades in 50+ ticks, AI adjusts params
        tick_count = agent.tick_count or 0
        if tick_count > 0 and tick_count % 50 == 0 and agent.session_trade_count == 0:
            try:
                nudge = await grok_analyst.nudge_stuck_agent(
                    agent_data={
                        "name": agent.name,
                        "strategy": agent.strategy.type if agent.strategy else None,
                        "strategy_params": agent.strategy_params or {},
                        "timeframe": agent.timeframe,
                        "trading_pairs": agent.trading_pairs,
                        "total_trades": agent.total_trades,
                        "tick_count": tick_count,
                    },
                    last_signals=["hold"] * 20,
                )
                if nudge and nudge.get("suggested_params"):
                    agent.strategy_params = {**(agent.strategy_params or {}), **nudge["suggested_params"]}
                    agent.last_error = f"AI nudge: {nudge.get('diagnosis', 'adjusting params')}"
                    logger.info("agent {} AI nudge: {}", agent.id, nudge.get("diagnosis"))
            except Exception:
                pass

        await agent.save()
        return {"ok": True}

    # Rough minimum qty per symbol — Bybit rejects orders below these sizes.
    _MIN_QTY: dict[str, float] = {
        "BTC": 0.001,
        "ETH": 0.01,
        "SOL": 0.1,
        "BNB": 0.01,
        "XRP": 1.0,
    }
    _DEFAULT_MIN_QTY = 0.01

    # Forex pairs have different minimums (in units of base currency)
    _FOREX_MIN_QTY = 1  # OANDA allows 1 unit minimum

    @classmethod
    def _min_qty_for(cls, symbol: str, exchange: str = "") -> float:
        if cls._is_forex(exchange):
            return cls._FOREX_MIN_QTY
        for prefix, min_q in cls._MIN_QTY.items():
            if symbol.upper().startswith(prefix):
                return min_q
        return cls._DEFAULT_MIN_QTY

    @staticmethod
    def _is_forex(exchange: str) -> bool:
        return exchange.startswith("mt5") or exchange.startswith("deriv") or exchange.startswith("oanda")

    # Symbols that persistently fail on all fallback providers — downgraded to
    # debug so they don't pollute logs on every tick.
    _KNOWN_UNAVAILABLE: frozenset[str] = frozenset({"TONUSDT"})

    async def _tick_symbol_ai(self, agent: Agent, api_key, strategy, client, symbol: str, df, screener_signal, open_position) -> None:
        """Process a symbol with AI analysis — called for top candidates only."""
        last_price = float(df["close"].iloc[-1])
        win_rate = (agent.winning_trades / agent.total_trades) if agent.total_trades > 0 else 0.5

        try:
            signal = await grok_analyst.analyse_market(
                screener_signal,
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
                    "in_position": open_position is not None,
                    "is_protect_mode": getattr(agent, "protect_mode", False),
                    "screener_said": screener_signal.action,
                    "screener_confidence": screener_signal.confidence,
                    "screener_reason": screener_signal.reason,
                },
            )
        except Exception as exc:
            logger.warning("agent {} {} AI failed: {}", agent.id, symbol, exc)
            signal = screener_signal

        agent.last_signal = signal.action
        agent.last_signal_symbol = symbol
        agent.last_error = None

        logger.debug(
            "agent {} {} signal={} price={:.4f} confidence={:.2f}",
            agent.id, symbol, signal.action, last_price, signal.confidence,
        )

        await self._execute_signal(agent, api_key, client, symbol, df, signal, open_position)

    async def _execute_signal(self, agent, api_key, client, symbol, df, signal, open_position) -> None:
        """Execute a trading signal (entry, exit, or hold)."""
        last_price = float(df["close"].iloc[-1])

        if signal.action == "hold":
            return

        if signal.action == "exit" and open_position is not None:
            await self._close_position(agent, api_key, client, open_position, last_price, signal.reason)
            return

        if signal.action in ("enter_long", "enter_short") and open_position is None:
            min_conf = 0.60 if agent.protect_mode else 0.40
            if signal.confidence < min_conf:
                agent.last_error = f"AI confidence {signal.confidence:.2f} < {min_conf}"
                return

            side: str = "long" if signal.action == "enter_long" else "short"

            # Use strategy defaults for SL/TP, not AI suggestions
            # AI can suggest tighter values but never looser than strategy defaults
            strat_params = agent.strategy_params or {}
            default_sl = float(strat_params.get("stop_loss_pct", 1.0))
            default_tp = float(strat_params.get("take_profit_pct",
                              strat_params.get("profit_target_pct", 2.5)))

            ai_sl = signal.suggested_stop_loss_pct or default_sl
            ai_tp = signal.suggested_take_profit_pct or default_tp

            # SL: use the TIGHTER of strategy default and AI suggestion
            sl_pct = min(ai_sl, default_sl)
            # TP: use the LARGER of strategy default and AI suggestion
            tp_pct = max(ai_tp, default_tp)

            # Enforce minimum 1.5:1 reward-to-risk ratio
            if tp_pct < sl_pct * 1.5:
                tp_pct = round(sl_pct * 1.5, 3)

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
            min_qty = self._min_qty_for(symbol, api_key.exchange if hasattr(api_key, 'exchange') else "")
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

    async def _close_all_positions(self, agent: Agent, api_key, client, reason: str) -> int:
        """Gracefully wind down open positions before a cooldown.

        Does NOT close abruptly. Instead:
        - Profitable positions: close immediately to lock in gains
        - Breakeven positions (within 0.1% of entry): close to free up capital
        - Losing positions that are near stop loss (>60% of SL distance): close to prevent further loss
        - Positions still running toward TP with room to profit: leave open
          with a tightened stop loss (moved to breakeven) so they can't turn
          into big losses while the agent is in cooldown
        """
        positions = await Position.find(
            Position.agent_id == agent.id,
            Position.is_open == True,  # noqa: E712
        ).to_list()

        if not positions:
            return 0

        # Fetch fresh prices for accurate P&L
        for pos in positions:
            try:
                raw = await client.get_candles(pos.symbol, agent.timeframe or "5m", limit=2)
                if raw:
                    from app.services.strategy.indicators import candles_to_df
                    df = candles_to_df(raw)
                    pos.current_price = float(df["close"].iloc[-1])
            except Exception:
                pass

        closed = 0
        tightened = 0
        for pos in positions:
            entry = float(pos.entry_price)
            current = float(pos.current_price or entry)
            sl = float(pos.stop_loss or entry)

            # Calculate unrealized P&L %
            if pos.side == "long":
                pnl_pct = (current - entry) / entry * 100
                distance_to_sl = (entry - sl) / entry * 100 if sl < entry else 0
                at_risk_pct = (entry - current) / entry * 100 if current < entry else 0
            else:
                pnl_pct = (entry - current) / entry * 100
                distance_to_sl = (sl - entry) / entry * 100 if sl > entry else 0
                at_risk_pct = (current - entry) / entry * 100 if current > entry else 0

            try:
                if pnl_pct > 0.05:
                    # In profit → close to lock in gains
                    await self._close_position(agent, api_key, client, pos, current, f"{reason} (locking profit +{pnl_pct:.2f}%)")
                    closed += 1
                elif abs(pnl_pct) <= 0.1:
                    # Breakeven → close to free capital
                    await self._close_position(agent, api_key, client, pos, current, f"{reason} (breakeven)")
                    closed += 1
                elif distance_to_sl > 0 and at_risk_pct > distance_to_sl * 0.6:
                    # Losing and close to stop loss → close before it gets worse
                    await self._close_position(agent, api_key, client, pos, current, f"{reason} (near SL, cutting loss)")
                    closed += 1
                else:
                    # Position still has room to run → tighten stop to breakeven
                    if pos.side == "long":
                        pos.stop_loss = max(entry, sl)
                    else:
                        pos.stop_loss = min(entry, sl) if sl > 0 else entry
                    await pos.save()
                    tightened += 1
                    logger.info(
                        "agent {} {}: tightened SL to breakeven (not closing — PnL {:.2f}%)",
                        agent.id, pos.symbol, pnl_pct,
                    )
            except Exception as exc:
                logger.warning("failed to handle position {} for agent {}: {}", pos.id, agent.id, exc)

        if closed or tightened:
            logger.info(
                "agent {}: {} position(s) closed, {} tightened to breakeven — {}",
                agent.id, closed, tightened, reason,
            )
        return closed

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

        # Session trade counter — triggers wind-down after N trades
        agent.session_trade_count = (agent.session_trade_count or 0) + 1
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

        # After 3 consecutive losses, AI re-optimizes params (agent keeps trading)
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

            if loss_streak >= 3:
                try:
                    await self._emergency_optimize(agent, position.symbol, loss_streak)
                except Exception as exc:
                    logger.warning("emergency optimize failed for {}: {}", agent.id, exc)

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

        # Propagate winning params to the entire fleet
        try:
            await LearningService.propagate_to_fleet(
                strategy_type=strategy_type,
                winning_params=new_params,
                source_agent_id=agent.id,
            )
        except Exception:
            pass

        logger.info(
            "agent {} emergency re-optimized + propagated to fleet: score={:.4f} params={}",
            agent.id, result.best_score, new_params,
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
            # Share the winning params with all agents using this strategy
            try:
                await LearningService.propagate_to_fleet(
                    strategy_type=strategy_type,
                    winning_params=new_params,
                    source_agent_id=agent.id,
                )
            except Exception:
                pass
            await NotificationService.create(
                user_id=agent.user_id,
                type="agent_optimized",
                title=f"{agent.name} learned from fleet — shared with all agents",
                message=f"Adopted winning params (realized PnL: ${winner.realized_pnl:.2f}). Propagated to all {strategy_type} agents.",
                data={"agent_id": str(agent.id), "trigger": "cooldown_study"},
            )
        else:
            logger.info("agent {} cooldown study: no fleet winner found, keeping current params", agent.id)
