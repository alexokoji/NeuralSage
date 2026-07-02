from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.models.agent import Agent
from app.models.agent_performance import AgentPerformance
from app.models.api_key import ApiKey
from app.models.position import Position
from app.models.user import User
from app.schemas.portfolio import (
    AgentDayPoint,
    AgentTrend,
    AgentTrendsResponse,
    BalanceEntry,
    ExchangeBalance,
    PortfolioOverview,
)
from app.services.exchange import build_client
from app.services.exchange.base import ExchangeError

router = APIRouter()


@router.get("/overview", response_model=PortfolioOverview)
async def overview(user: User = Depends(get_current_user)) -> PortfolioOverview:
    keys = await ApiKey.find(ApiKey.user_id == user.id).to_list()

    exchanges: list[ExchangeBalance] = []
    total_usd = 0.0
    for k in keys:
        if not k.is_active:
            continue
        fetch_error: str | None = None
        balances = []
        try:
            client = build_client(k)
            try:
                balances = await client.get_balances()
            finally:
                await client.close()
        except (ExchangeError, PermissionError) as exc:
            fetch_error = str(exc)
            k.balance_cache = {"error": fetch_error}
            await k.save()

        if fetch_error:
            exchanges.append(
                ExchangeBalance(
                    api_key_id=str(k.id),
                    exchange=k.exchange,
                    is_testnet=k.is_testnet,
                    balances=[],
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    error=fetch_error,
                )
            )
            continue

        entries = [
            BalanceEntry(asset=b.asset, available=b.available, total=b.total, usd_value=b.usd_value)
            for b in balances
        ]
        exch_total = sum((b.usd_value or (b.total if b.asset.upper() == "USDT" else 0)) for b in balances)
        total_usd += exch_total
        k.balance_cache = {b.asset: float(b.total) for b in balances}
        k.balance_updated_at = datetime.now(timezone.utc)
        await k.save()
        exchanges.append(
            ExchangeBalance(
                api_key_id=str(k.id),
                exchange=k.exchange,
                is_testnet=k.is_testnet,
                balances=entries,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    open_positions_count = await Position.find(
        Position.user_id == user.id,
        Position.is_open == True,  # noqa: E712
    ).count()

    active_agents_count = await Agent.find(
        Agent.user_id == user.id,
        Agent.status == "active",
    ).count()

    agents = await Agent.find(Agent.user_id == user.id).to_list()
    pnl_total = sum(float(a.total_pnl or 0) for a in agents)
    pnl_today = sum(float(a.current_day_pnl or 0) for a in agents)

    cap_basis = total_usd if total_usd > 0 else 1.0
    return PortfolioOverview(
        total_balance_usd=float(total_usd),
        total_pnl=float(pnl_total),
        total_pnl_pct=float(pnl_total) / cap_basis * 100,
        daily_pnl=float(pnl_today),
        daily_pnl_pct=float(pnl_today) / cap_basis * 100,
        open_positions_count=int(open_positions_count),
        active_agents_count=int(active_agents_count),
        exchanges=exchanges,
    )


@router.get("/agent-trends", response_model=AgentTrendsResponse)
async def agent_trends(
    days: int = 7,
    user: User = Depends(get_current_user),
) -> AgentTrendsResponse:
    """Return per-agent daily PnL for the last N days from AgentPerformance snapshots."""
    from datetime import date, timedelta

    cutoff = date.today() - timedelta(days=days - 1)
    agents = await Agent.find(Agent.user_id == user.id).to_list()
    trends: list[AgentTrend] = []

    for agent in agents:
        snaps = await AgentPerformance.find(
            AgentPerformance.agent_id == agent.id,
            AgentPerformance.snapshot_date >= cutoff,
        ).sort(AgentPerformance.snapshot_date).to_list()

        if not snaps:
            continue

        points = [
            AgentDayPoint(
                date=str(s.snapshot_date),
                daily_pnl=float(s.daily_pnl),
                daily_pnl_pct=float(s.daily_pnl_pct),
            )
            for s in snaps
        ]
        trends.append(AgentTrend(
            agent_id=str(agent.id),
            agent_name=agent.name or f"Agent {str(agent.id)[:6]}",
            points=points,
        ))

    return AgentTrendsResponse(trends=trends)
