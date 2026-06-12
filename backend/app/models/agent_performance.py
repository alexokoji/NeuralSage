from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any, Optional

from beanie import Document, Indexed
from pydantic import Field


class AgentPerformance(Document):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    agent_id: Annotated[uuid.UUID, Indexed()]
    user_id: Annotated[uuid.UUID, Indexed()]
    snapshot_date: date = Field(default_factory=date.today)

    starting_capital: float = 0.0
    ending_capital: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    max_drawdown: float = 0.0
    sharpe_ratio: Optional[float] = None
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    strategy_params_snapshot: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "agent_performance"
