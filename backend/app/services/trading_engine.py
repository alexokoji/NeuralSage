"""Trading Engine.

Pipeline for one tick of one agent:
  1. Pull candles from the agent's exchange.
  2. Ask the strategy for a signal.
  3. If the signal is enter/exit:
     a. Run RiskEngine.evaluate_entry (entries only).
     b. Place the order via the exchange client.
     c. Persist a Trade row + (for entries) a Position row.
     d. Update agent counters and emit a notification.
  4. If no actionable signal, persist nothing.

Every trade is logged with the risk_checks payload that approved it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.api_key import ApiKey
from app.models.position import Position
from app.models.trade import Trade
from app.services.exchange import OrderRequest, build_client
from app.services.exchange.base import ExchangeError
import app.services.grok_analyst as grok_analyst
from app.services.notifications import NotificationService
from app.services.risk_engine import RiskEngine
from app.services.strategy import StrategyContext, get_strategy
from app.services.strategy.indicators import candles_to_df


class TradingEngine:
    """One-shot per-agent execution. Designed for the Celery worker tick."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def run_agent_tick(self, agent: Agent, api_key: ApiKey) -> dict[str, Any]:
        if agent.status != "active":
            return {"skipped": True, "reason": f"agent {agent.status}"}

        strategy_type = agent.strategy.type if agent.strategy else None
        if not strategy_type:
            return {"skipped": True, "reason": "agent has no strategy"}

        strategy = get_strategy(strategy_type)
        client = build_client(api_key)
        try:
            for symbol in (agent.trading_pairs or []):
                await self._tick_symbol(agent, api_key, strategy, client, symbol)
        finally:
            await client.close()
        return {"ok": True}

    async def _tick_symbol(self, agent: Agent, api_key, strategy, client, symbol: str) -> None:
        # 1) Candles
        try:
            raw = await client.get_candles(symbol, agent.timeframe, limit=200)
        except ExchangeError as exc:
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
        signal = strategy.evaluate(df, agent.strategy_params or {}, ctx)

        # --- Grok AI validation: review signal against live candle context ---
        signal = await grok_analyst.validate_signal(
            signal,
            df,
            symbol=symbol,
            timeframe=agent.timeframe,
            strategy_type=strategy.type,
            strategy_params=agent.strategy_params or {},
            agent_context={
                "agent_id": str(agent.id),
                "total_trades": agent.total_trades,
                "total_pnl": float(agent.total_pnl or 0),
                "confidence_score": float(agent.confidence_score or 50),
                "in_position": ctx.in_position,
            },
        )
        # ---------------------------------------------------------------------

        last_price = float(df["close"].iloc[-1])

        # Hold / no action.
        if signal.action == "hold":
            return

        # 2) Exit handling.
        if signal.action == "exit" and open_position is not None:
            await self._close_position(agent, api_key, client, open_position, last_price, signal.reason)
            return

        # 3) Entry: risk-engine gating.
        if signal.action in ("enter_long", "enter_short") and open_position is None:
            side: str = "long" if signal.action == "enter_long" else "short"
            sl_pct = signal.suggested_stop_loss_pct or 1.5
            tp_pct = signal.suggested_take_profit_pct or 3.0

            decision = await RiskEngine.evaluate_entry(
                self.db,
                agent,
                entry_price=last_price,
                stop_loss_pct=sl_pct,
                side=side,  # type: ignore[arg-type]
            )
            if not decision.approved:
                await RiskEngine.log_risk_event(
                    self.db,
                    user_id=agent.user_id,
                    agent_id=agent.id,
                    event_type=decision.code if decision.code != "ok" else "api_error",
                    severity="warning",
                    message=decision.reason,
                    details={"symbol": symbol, "signal": signal.__dict__},
                )
                return

            await self._open_trade(
                agent=agent,
                api_key=api_key,
                client=client,
                symbol=symbol,
                side=side,  # type: ignore[arg-type]
                entry_price=last_price,
                quantity=decision.sized_quantity,
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

    async def _open_position(self, agent_id, symbol: str) -> Position | None:
        from sqlalchemy import select

        result = await self.db.execute(
            select(Position).where(
                Position.agent_id == agent_id,
                Position.symbol == symbol,
                Position.is_open.is_(True),
            )
        )
        return result.scalar_one_or_none()

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
        try:
            placed = await client.place_order(order)
        except ExchangeError as exc:
            await RiskEngine.log_risk_event(
                self.db,
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
        self.db.add(trade)
        await self.db.flush()
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
        self.db.add(position)

        agent.total_trades = (agent.total_trades or 0) + 1
        agent.last_trade_at = datetime.now(timezone.utc)
        agent.confidence_score = max(0, min(100, 50 + (signal.confidence - 0.5) * 100))

        await self.db.commit()

        await NotificationService.create(
            self.db,
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
        try:
            await client.place_order(order)
        except ExchangeError as exc:
            await RiskEngine.log_risk_event(
                self.db,
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

        # Locate the originating trade and close it.
        from sqlalchemy import select

        trade_result = await self.db.execute(select(Trade).where(Trade.id == position.trade_id))
        trade = trade_result.scalar_one_or_none()
        if trade:
            trade.exit_price = last_price
            trade.pnl = gross
            trade.pnl_pct = (gross / max(entry_price * qty, 1e-9)) * 100
            trade.status = "filled"
            trade.closed_at = datetime.now(timezone.utc)
            trade.notes = reason

        agent.total_pnl = float(agent.total_pnl or 0) + gross
        agent.current_day_pnl = float(agent.current_day_pnl or 0) + gross
        agent.current_week_pnl = float(agent.current_week_pnl or 0) + gross
        if gross > 0:
            agent.winning_trades = (agent.winning_trades or 0) + 1

        await self.db.commit()

        await NotificationService.create(
            self.db,
            user_id=agent.user_id,
            type="trade_closed",
            title=f"{agent.name} closed {position.side} {position.symbol}",
            message=f"PnL {gross:+.2f} ({reason})",
            data={"agent_id": str(agent.id), "trade_id": str(position.trade_id)},
        )
