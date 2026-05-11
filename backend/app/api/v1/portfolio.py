from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
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
async def overview(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PortfolioOverview:
    keys = (await db.execute(select(ApiKey).where(ApiKey.user_id == user.id))).scalars().all()

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
            # Surface as empty balance set; UI shows the error from /verify.
            balances = []
            k.balance_cache = {"error": str(exc)}
            await db.commit()
            continue

        entries = [
            BalanceEntry(asset=b.asset, available=b.available, total=b.total, usd_value=b.usd_value)
            for b in balances
        ]
        exch_total = sum((b.usd_value or (b.total if b.asset.upper() == "USDT" else 0)) for b in balances)
        total_usd += exch_total
        k.balance_cache = {b.asset: float(b.total) for b in balances}
        k.balance_updated_at = datetime.now(timezone.utc)
        exchanges.append(
            ExchangeBalance(
                api_key_id=str(k.id),
                exchange=k.exchange,
                is_testnet=k.is_testnet,
                balances=entries,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
    await db.commit()

    open_positions_count = (
        await db.execute(
            select(func.count(Position.id)).where(
                Position.user_id == user.id, Position.is_open.is_(True)
            )
        )
    ).scalar_one()
    active_agents_count = (
        await db.execute(
            select(func.count(Agent.id)).where(Agent.user_id == user.id, Agent.status == "active")
        )
    ).scalar_one()

    pnl_total = (
        await db.execute(select(func.coalesce(func.sum(Agent.total_pnl), 0)).where(Agent.user_id == user.id))
    ).scalar_one() or 0.0
    pnl_today = (
        await db.execute(
            select(func.coalesce(func.sum(Agent.current_day_pnl), 0)).where(Agent.user_id == user.id)
        )
    ).scalar_one() or 0.0

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
