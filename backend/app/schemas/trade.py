from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop_loss", "take_profit"]
TradeStatus = Literal["pending", "open", "filled", "cancelled", "rejected", "expired"]


class TradePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    agent_id: uuid.UUID | None
    api_key_id: uuid.UUID | None
    exchange: str
    exchange_order_id: str | None
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: TradeStatus
    quantity: float
    entry_price: float | None
    exit_price: float | None
    stop_loss: float | None
    take_profit: float | None
    pnl: float
    pnl_pct: float
    fees: float
    signal_source: str
    signal_data: dict[str, Any]
    risk_checks: dict[str, Any]
    notes: str
    opened_at: datetime
    closed_at: datetime | None


class ManualOrderRequest(BaseModel):
    api_key_id: uuid.UUID
    symbol: str = Field(min_length=3, max_length=32)
    side: OrderSide
    order_type: OrderType = "market"
    quantity: float = Field(gt=0)
    price: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)


class PositionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID | None
    exchange: str
    symbol: str
    side: Literal["long", "short"]
    quantity: float
    entry_price: float
    current_price: float | None
    stop_loss: float | None
    take_profit: float | None
    unrealized_pnl: float
    unrealized_pnl_pct: float
    leverage: float
    margin_used: float
    is_open: bool
    opened_at: datetime
    updated_at: datetime
