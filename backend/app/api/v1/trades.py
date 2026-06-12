from __future__ import annotations

import uuid
from datetime import datetime, timezone

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
    user: User = Depends(get_current_user),
):
    return await Trade.find(Trade.user_id == user.id).sort(-Trade.created_at).limit(limit).to_list()


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
