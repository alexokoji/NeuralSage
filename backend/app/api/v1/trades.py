from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_current_user
from app.models.api_key import ApiKey
from app.models.position import Position
from app.models.trade import Trade
from app.models.user import User
from app.schemas.trade import (
    ManualOrderRequest,
    PositionPublic,
    TradePublic,
)
from app.services.exchange import OrderRequest, build_client
from app.services.exchange.base import ExchangeError

router = APIRouter()


@router.get("", response_model=list[TradePublic])
async def list_trades(
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None),
    user: User = Depends(get_current_user),
):
    query_status = status or "filled"
    if query_status == "all":
        return await Trade.find(Trade.user_id == user.id).sort(-Trade.created_at).limit(limit).to_list()
    return await Trade.find(
        Trade.user_id == user.id,
        Trade.status == query_status,
    ).sort(-Trade.created_at).limit(limit).to_list()


@router.get("/positions", response_model=list[PositionPublic])
async def list_positions(user: User = Depends(get_current_user)):
    return await Position.find(
        Position.user_id == user.id,
        Position.is_open == True,  # noqa: E712
    ).sort(-Position.opened_at).to_list()


@router.post("/manual", response_model=TradePublic, status_code=status.HTTP_201_CREATED)
async def place_manual_order(
    body: ManualOrderRequest,
    user: User = Depends(get_current_user),
):
    api_key = await ApiKey.find_one(ApiKey.id == body.api_key_id, ApiKey.user_id == user.id)
    if not api_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid api_key_id")
    if "trade" not in (api_key.permissions or []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "api key cannot trade")

    client = build_client(api_key)
    try:
        try:
            placed = await client.place_order(
                OrderRequest(
                    symbol=body.symbol,
                    side=body.side,
                    order_type=body.order_type,
                    quantity=body.quantity,
                    price=body.price,
                    stop_loss=body.stop_loss,
                    take_profit=body.take_profit,
                    client_order_id=f"manual-{uuid.uuid4().hex[:8]}",
                )
            )
        except (ExchangeError, PermissionError) as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    finally:
        await client.close()

    trade = Trade(
        user_id=user.id,
        api_key_id=api_key.id,
        exchange=api_key.exchange,
        exchange_order_id=placed.exchange_order_id,
        symbol=body.symbol,
        side=body.side,
        order_type=body.order_type,
        status="open",
        quantity=body.quantity,
        entry_price=body.price,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        signal_source="manual",
        signal_data={"placed_by": str(user.id)},
        risk_checks={"manual": True},
        opened_at=datetime.now(timezone.utc),
    )
    await trade.insert()
    return trade


@router.post("/cleanup-positions", status_code=status.HTTP_200_OK)
async def cleanup_stale_positions(user: User = Depends(get_current_user)):
    """Close open DB positions that no longer exist on the exchange (or are old paper trades).

    - Live positions: checks Bitget; closes any DB position not found there.
    - Paper positions: closes any position open more than 24 hours (SL/TP should have fired).
    Safe to call multiple times.
    """
    from datetime import timedelta
    from app.models.agent import Agent
    from app.services.exchange import build_client
    from app.services.exchange.base import ExchangeError

    open_positions = await Position.find(
        Position.user_id == user.id,
        Position.is_open == True,  # noqa: E712
    ).to_list()

    closed = 0
    now = datetime.now(timezone.utc)

    # Group positions by agent to share one exchange client per agent
    agent_cache: dict[str, Any] = {}
    exchange_symbols_cache: dict[str, set] = {}

    for pos in open_positions:
        agent_id_str = str(pos.agent_id) if pos.agent_id else None

        # Determine if paper or live
        is_paper = True
        if agent_id_str and agent_id_str not in agent_cache:
            agent = await Agent.get(pos.agent_id)
            agent_cache[agent_id_str] = agent
        agent = agent_cache.get(agent_id_str) if agent_id_str else None
        if agent:
            is_paper = bool(agent.is_paper_trade)

        if is_paper:
            # Paper position open > 24h means SL/TP never fired — stale
            age = (now - pos.opened_at).total_seconds() if pos.opened_at else 999999
            if age > 86400:
                pos.is_open = False
                pos.updated_at = now
                await pos.save()
                closed += 1
            continue

        # Live position: check if it still exists on the exchange
        api_key_id_str = str(pos.agent_id)  # use agent to find api key
        if agent_id_str not in exchange_symbols_cache:
            try:
                if agent and agent.api_key_id:
                    api_key = await ApiKey.get(agent.api_key_id)
                    client = build_client(api_key)
                    try:
                        exchange_positions = await client.get_positions()
                        exchange_symbols_cache[agent_id_str] = {
                            str(p.get("symbol", "")).upper() for p in (exchange_positions or [])
                        }
                    finally:
                        await client.close()
                else:
                    exchange_symbols_cache[agent_id_str] = set()
            except Exception:
                exchange_symbols_cache[agent_id_str] = None  # type: ignore

        live_symbols = exchange_symbols_cache.get(agent_id_str)
        if live_symbols is None:
            continue  # couldn't fetch — skip rather than falsely close

        if str(pos.symbol).upper() not in live_symbols:
            pos.is_open = False
            pos.updated_at = now
            await pos.save()
            closed += 1

    return {"open_before": len(open_positions), "closed": closed, "remaining": len(open_positions) - closed}


@router.post("/cleanup-stale", status_code=status.HTTP_200_OK)
async def cleanup_stale_trades(user: User = Depends(get_current_user)):
    """Mark open trades that have no matching open position as cancelled.

    Safe to call multiple times. Removes ghost records left by bugs.
    """
    from datetime import timedelta
    open_trades = await Trade.find(
        Trade.user_id == user.id,
        Trade.status == "open",
    ).to_list()

    open_position_trade_ids = {
        str(p.trade_id)
        for p in await Position.find(
            Position.user_id == user.id,
            Position.is_open == True,  # noqa: E712
        ).to_list()
        if p.trade_id
    }

    purged = 0
    for t in open_trades:
        if str(t.id) not in open_position_trade_ids:
            t.status = "cancelled"
            t.closed_at = datetime.now(timezone.utc)
            t.notes = "auto-cancelled: no matching open position"
            await t.save()
            purged += 1

    return {"purged": purged, "remaining_open": len(open_trades) - purged}


@router.post("/{trade_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_trade(trade_id: uuid.UUID, user: User = Depends(get_current_user)):
    trade = await Trade.find_one(Trade.id == trade_id, Trade.user_id == user.id)
    if not trade:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "trade not found")
    if trade.status not in ("open", "pending"):
        raise HTTPException(status.HTTP_409_CONFLICT, "trade is not cancellable")
    if not trade.api_key_id or not trade.exchange_order_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing exchange data")

    api_key = await ApiKey.get(trade.api_key_id)
    client = build_client(api_key)
    try:
        try:
            await client.cancel_order(trade.symbol, trade.exchange_order_id)
        except (ExchangeError, PermissionError) as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    finally:
        await client.close()

    trade.status = "cancelled"
    trade.closed_at = datetime.now(timezone.utc)
    await trade.save()
