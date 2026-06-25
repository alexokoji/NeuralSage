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

        # --- Winding down: no new entries, but still manage open positions ---
        if agent.winding_down:
            open_count = await Position.find(
                Position.agent_id == agent.id,
                Position.is_open == True,  # noqa: E712
            ).count()
            if open_count > 0:
                # Still have open trades — keep ticking to process exit signals
                agent.last_tick_at = now
                agent.last_error = f"winding down: {open_count} trade(s) still open, waiting for TP/SL"
                agent.tick_count = (agent.tick_count or 0) + 1
                await agent.save()
                # Continue to symbol loop so exit signals can fire
            else:
                # All positions closed — start cooldown
                from datetime import timedelta
                hours = agent.cooldown_hours or 1.0
                agent.winding_down = False
                agent.cooldown_until = now + timedelta(hours=hours)
                agent.last_error = None
                await agent.save()
                logger.info("agent {} all positions closed — starting {:.0f}h cooldown", agent.id, hours)
                try:
                    await self._study_fleet_and_adapt(agent)
                except Exception:
                    pass
                await NotificationService.create(
                    user_id=agent.user_id,
                    type="agent_status",
                    title=f"{agent.name} all trades finished — studying for {hours:.0f}h",
                    message=f"Open trades completed. Studying fleet data. Auto-resumes in {hours:.0f}h.",
                    data={"agent_id": str(agent.id), "trigger": "cooldown_start"},
                )
                return {"skipped": True, "reason": "cooldown started after wind-down"}

        # --- Session cooldown: auto-resume when time is up ---
        if agent.cooldown_until:
            # Handle timezone-naive datetimes from DB
            cooldown_dt = agent.cooldown_until
            if cooldown_dt.tzinfo is None:
                cooldown_dt = cooldown_dt.replace(tzinfo=timezone.utc)

            if now < cooldown_dt:
                remaining = (cooldown_dt - now).total_seconds() / 60
                agent.last_error = f"studying fleet data ({remaining:.0f}m remaining)"
                agent.last_tick_at = now
                await agent.save()
                return {"skipped": True, "reason": f"cooldown ({remaining:.0f}m left)"}
            else:
                # Cooldown expired — auto-resume immediately
                logger.info(
                    "agent {} cooldown expired (was until {}, now {}) — auto-resuming",
                    agent.id, cooldown_dt.isoformat(), now.isoformat(),
                )
                agent.cooldown_until = None
                agent.session_trade_count = 0
                agent.winding_down = False
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

        # --- Daily profit protection ---
        # If the agent has made >= X% of capital TODAY, enter protect mode
        # for the rest of the day. Resets at midnight via daily rollover.
        capital = float(agent.assigned_capital or 0)
        if capital > 0:
            day_pnl_pct = (float(agent.current_day_pnl or 0) / capital) * 100
            protect_threshold = float(agent.profit_protect_pct or 15)
            if day_pnl_pct >= protect_threshold and not agent.protect_mode:
                agent.protect_mode = True
                agent.winding_down = True
                logger.info(
                    "agent {} daily profit protection: today's PnL {:.1f}% >= {:.1f}% target — winding down",
                    agent.id, day_pnl_pct, protect_threshold,
                )
                await NotificationService.create(
                    user_id=agent.user_id,
                    type="agent_status",
                    title=f"{agent.name} hit daily target ({day_pnl_pct:.1f}% today)",
                    message=f"Protecting today's gains. Letting open trades finish. Resets tomorrow.",
                    data={"agent_id": str(agent.id), "trigger": "daily_profit_protect"},
                )

        # --- AI win-rate watchdog: if win rate is poor, force a study break ---
        total_t = agent.total_trades or 0
        if total_t >= 5:
            win_rate = (agent.winning_trades or 0) / total_t
            pnl = float(agent.total_pnl or 0)
            if win_rate < 0.35 and pnl < 0 and not agent.cooldown_until and not agent.winding_down:
                agent.winding_down = True
                agent.last_error = f"AI watchdog: win rate {win_rate:.0%} — winding down open trades"
                await agent.save()
                logger.warning(
                    "agent {} AI watchdog: win_rate={:.0%} pnl={:.2f} — winding down before cooldown",
                    agent.id, win_rate, pnl,
                )
                await NotificationService.create(
                    user_id=agent.user_id,
                    type="agent_status",
                    title=f"{agent.name} winding down (poor win rate)",
                    message=f"Win rate {win_rate:.0%}. No new entries — letting open trades finish before study break.",
                    data={"agent_id": str(agent.id), "trigger": "ai_watchdog"},
                )

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

        # Stuck agent nudge: if no trades in 50+ ticks, ask Groq to diagnose
        ticks_since_trade = (agent.tick_count or 0)
        if agent.last_trade_at:
            ticks_since_start = (agent.tick_count or 0)
        else:
            ticks_since_start = ticks_since_trade

        if (
            ticks_since_trade > 0
            and ticks_since_trade % 50 == 0
            and agent.session_trade_count == 0
            and not agent.winding_down
            and not agent.cooldown_until
        ):
            try:
                nudge = await grok_analyst.nudge_stuck_agent(
                    agent_data={
                        "name": agent.name,
                        "strategy": agent.strategy.type if agent.strategy else None,
                        "strategy_params": agent.strategy_params or {},
                        "timeframe": agent.timeframe,
                        "trading_pairs": agent.trading_pairs,
                        "total_trades": agent.total_trades,
                        "tick_count": agent.tick_count,
                    },
                    last_signals=["hold"] * 20,
                )
                if nudge and nudge.get("suggested_params"):
                    old_params = dict(agent.strategy_params or {})
                    agent.strategy_params = {**old_params, **nudge["suggested_params"]}
                    agent.last_error = f"AI nudge: {nudge.get('diagnosis', 'adjusting params')}"
                    logger.info(
                        "agent {} stuck-agent nudge applied: {} → {}",
                        agent.id, nudge.get("diagnosis"), nudge.get("suggested_params"),
                    )
                    await NotificationService.create(
                        user_id=agent.user_id,
                        type="agent_status",
                        title=f"{agent.name} — AI adjusted params (no trades in {ticks_since_trade} ticks)",
                        message=nudge.get("diagnosis", "Parameters adjusted to improve trade detection."),
                        data={"agent_id": str(agent.id), "trigger": "stuck_nudge"},
                    )
            except Exception as exc:
                logger.debug("stuck agent nudge failed: {}", exc)

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
        if exchange.startswith("oanda"):
            return cls._FOREX_MIN_QTY
        for prefix, min_q in cls._MIN_QTY.items():
            if symbol.upper().startswith(prefix):
                return min_q
        return cls._DEFAULT_MIN_QTY

    @staticmethod
    def _is_forex(exchange: str) -> bool:
        return exchange.startswith("oanda")

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
        last_price = float(df["close"].iloc[-1])

        # --- Auto TP/SL: check if price hit stop loss or take profit ---
        # The exchange handles this for live trades, but paper trades need
        # the engine to check manually every tick.
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
                close_price = tp
                await self._close_position(agent, api_key, client, open_position, close_price, "take profit hit")
                agent.last_signal = "exit"
                agent.last_signal_symbol = f"{symbol} TP"
                return
            if hit_sl:
                close_price = sl
                await self._close_position(agent, api_key, client, open_position, close_price, "stop loss hit")
                agent.last_signal = "exit"
                agent.last_signal_symbol = f"{symbol} SL"
                return

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
            # Winding down — no new entries, let existing trades finish
            if getattr(agent, "winding_down", False):
                return

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
        max_session = agent.trades_per_session or 10
        if agent.session_trade_count >= max_session and not agent.winding_down:
            agent.winding_down = True
            logger.info(
                "agent {} completed session ({} trades) — winding down, letting open trades finish",
                agent.id, agent.session_trade_count,
            )
            await NotificationService.create(
                user_id=agent.user_id,
                type="agent_status",
                title=f"{agent.name} winding down ({agent.session_trade_count} trades done)",
                message="No new entries. Waiting for open trades to reach TP/SL before study break.",
                data={"agent_id": str(agent.id), "trigger": "session_wind_down"},
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
