"""Risk Management Engine — MongoDB/Beanie edition.

Hard caps (enforced even when an agent is configured looser):
  * MAX_RISK_PER_TRADE_PCT   (% of agent capital exposed to a stop-loss)
  * MAX_DAILY_DRAWDOWN_PCT   (% of agent capital lost since 00:00 UTC)
  * MAX_CONCURRENT_TRADES
  * MAX_CONSECUTIVE_LOSSES   (recent streak)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.config import settings
from app.models.agent import Agent
from app.models.position import Position
from app.models.risk_event import RiskEvent
from app.models.trade import Trade


@dataclass
class RiskDecision:
    approved: bool
    reason: str = ""
    code: Literal[
        "ok",
        "max_risk_per_trade",
        "max_daily_drawdown",
        "position_limit",
        "consecutive_losses",
        "agent_inactive",
        "no_capital",
        "invalid_stop",
    ] = "ok"
    sized_quantity: float = 0.0
    risk_amount: float = 0.0


class RiskEngine:
    """Stateless evaluator — every method takes everything it needs explicitly."""

    @staticmethod
    def cap_risk_per_trade(agent: Agent) -> float:
        agent_pct = float(agent.max_risk_per_trade or 0)
        return min(agent_pct, settings.MAX_RISK_PER_TRADE_PCT)

    @staticmethod
    def position_size(
        agent: Agent,
        *,
        entry_price: float,
        stop_loss_price: float,
    ) -> tuple[float, float]:
        if entry_price <= 0 or stop_loss_price <= 0 or entry_price == stop_loss_price:
            return 0.0, 0.0
        capital = float(agent.assigned_capital or 0)
        if capital <= 0:
            return 0.0, 0.0
        risk_pct = RiskEngine.cap_risk_per_trade(agent) / 100
        dollars_at_risk = capital * risk_pct
        per_unit_loss = abs(entry_price - stop_loss_price)
        qty = dollars_at_risk / per_unit_loss
        return max(qty, 0.0), dollars_at_risk

    @classmethod
    async def evaluate_entry(
        cls,
        agent: Agent,
        *,
        entry_price: float,
        stop_loss_pct: float,
        side: Literal["long", "short"],
    ) -> RiskDecision:
        if agent.status not in ("active",):
            return RiskDecision(False, f"agent status is {agent.status}", "agent_inactive")

        if (agent.assigned_capital or 0) <= 0:
            return RiskDecision(False, "agent has no assigned capital", "no_capital")

        if stop_loss_pct <= 0:
            return RiskDecision(False, "stop loss must be > 0", "invalid_stop")

        open_count = await cls._open_position_count(agent.id)
        per_agent_cap = min(agent.max_concurrent_trades or 1, settings.MAX_CONCURRENT_TRADES)
        if open_count >= per_agent_cap:
            return RiskDecision(
                False,
                f"already at concurrent trade cap ({open_count}/{per_agent_cap})",
                "position_limit",
            )

        streak = await cls._consecutive_loss_streak(agent.id)
        max_streak = min(agent.max_consecutive_losses or 99, settings.MAX_CONSECUTIVE_LOSSES)
        if streak >= max_streak:
            return RiskDecision(
                False,
                f"consecutive losses ({streak}) reached limit ({max_streak})",
                "consecutive_losses",
            )

        if (agent.assigned_capital or 0) > 0:
            day_pnl = float(agent.current_day_pnl or 0)
            day_pct = (day_pnl / float(agent.assigned_capital)) * 100
            cap = -min(agent.max_daily_loss or 100, settings.MAX_DAILY_DRAWDOWN_PCT)
            if day_pct <= cap:
                return RiskDecision(
                    False,
                    f"daily drawdown {day_pct:.2f}% breaches limit {cap:.2f}%",
                    "max_daily_drawdown",
                )

        sl_price = (
            entry_price * (1 - stop_loss_pct / 100)
            if side == "long"
            else entry_price * (1 + stop_loss_pct / 100)
        )
        qty, risk_dollars = cls.position_size(agent, entry_price=entry_price, stop_loss_price=sl_price)
        if qty <= 0:
            return RiskDecision(False, "position sized to zero", "max_risk_per_trade")

        return RiskDecision(True, "ok", "ok", sized_quantity=qty, risk_amount=risk_dollars)

    @staticmethod
    async def _open_position_count(agent_id) -> int:
        return await Position.find(
            Position.agent_id == agent_id,
            Position.is_open == True,  # noqa: E712
        ).count()

    @staticmethod
    async def _consecutive_loss_streak(agent_id) -> int:
        trades = await Trade.find(
            Trade.agent_id == agent_id,
            Trade.status == "filled",
            Trade.closed_at != None,  # noqa: E711
        ).sort(-Trade.closed_at).limit(10).to_list()
        streak = 0
        for t in trades:
            if float(t.pnl) < 0:
                streak += 1
            else:
                break
        return streak

    @staticmethod
    async def log_risk_event(
        *,
        user_id,
        agent_id,
        event_type: str,
        severity: str,
        message: str,
        details: dict | None = None,
    ) -> None:
        await RiskEvent(
            user_id=user_id,
            agent_id=agent_id,
            event_type=event_type,
            severity=severity,
            message=message,
            details=details or {},
            created_at=datetime.now(timezone.utc),
        ).insert()
