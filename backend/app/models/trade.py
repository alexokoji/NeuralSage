from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from beanie import Document, Indexed
from pydantic import Field


class Trade(Document):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    user_id: Annotated[uuid.UUID, Indexed()]
    agent_id: Annotated[Optional[uuid.UUID], Indexed()] = None
    api_key_id: Optional[uuid.UUID] = None

    exchange: str
    exchange_order_id: Optional[str] = None
    symbol: str
    side: str
    order_type: str
    status: str = "pending"

    quantity: float
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None

    pnl: float = 0.0
    pnl_pct: float = 0.0
    fees: float = 0.0

    signal_source: str = "strategy"
    signal_data: dict[str, Any] = Field(default_factory=dict)
    risk_checks: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""

    # Market condition at entry: "trending_up" | "trending_down" | "ranging" | None
    # Derived from the strategy's trend filter or the AI's market_structure tag.
    # Stored here (not buried in signal_data) so the coach agent can query by regime.
    market_regime: Optional[str] = None

    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
    created_at: Annotated[datetime, Indexed()] = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "trades"
