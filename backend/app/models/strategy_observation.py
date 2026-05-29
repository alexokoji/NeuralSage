from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StrategyObservation(Base):
    """A single recorded outcome of running a strategy with a parameter set.

    Used by the AI optimizer to warm-start Bayesian search across ALL users'
    agents — so when agent B starts optimizing the same strategy on the same
    symbol/timeframe as agent A, the optimizer doesn't start blind: it begins
    from the best param sets agent A discovered.

    `trust_score` is computed at query time from `backtest_score`, `realized_pnl`,
    and `realized_trades` so trades that actually made money carry more weight
    than backtest-only observations.
    """

    __tablename__ = "strategy_observations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    strategy_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)

    # Source attribution (nullable so deleting an agent doesn't lose the knowledge).
    source_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )
    source_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    backtest_score: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)

    # Updated post-hoc as the agent trades under these params.
    realized_pnl: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    realized_trades: Mapped[int] = mapped_column(Integer, default=0)

    # Backtest window the score was computed over.
    candle_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    candle_window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_obs_lookup", "strategy_type", "symbol", "timeframe", "created_at"),
    )
