from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from beanie import Document, Indexed
from pydantic import Field


class StrategyObservation(Document):
    """Recorded outcome of running a strategy with a parameter set.

    Used by the AI optimizer as fleet-wide warm starts. Real-money outcomes
    outweigh backtest-only observations via the trust_score in learning.py.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    strategy_type: Annotated[str, Indexed()]
    symbol: Annotated[str, Indexed()]
    timeframe: str

    source_agent_id: Optional[uuid.UUID] = None
    source_user_id: Optional[uuid.UUID] = None

    params: dict[str, Any]
    backtest_score: float

    realized_pnl: float = 0.0
    realized_trades: int = 0

    candle_window_start: Optional[datetime] = None
    candle_window_end: Optional[datetime] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "strategy_observations"
