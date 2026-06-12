from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.models.agent import Agent
from app.models.api_key import ApiKey
from app.models.position import Position
from app.models.user import User
from app.schemas.portfolio import (
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
        try:
            client = build_client(k)
            try:
                balances = await client.get_balances()
            finally:
                await client.close()
        except (ExchangeError, PermissionError) as exc:
            balances = []
            k.balance_cache = {"error": str(exc)}
            await k.save()
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
