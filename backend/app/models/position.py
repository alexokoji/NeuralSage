from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Optional

from beanie import Document, Indexed
from pydantic import Field


class Position(Document):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    user_id: Annotated[uuid.UUID, Indexed()]
    agent_id: Optional[uuid.UUID] = None
    trade_id: Optional[uuid.UUID] = None

    exchange: str
    symbol: str
    side: str

    quantity: float
    entry_price: float
    current_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None

    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    leverage: float = 1.0
    margin_used: float = 0.0
    is_open: Annotated[bool, Indexed()] = True

    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "positions"
