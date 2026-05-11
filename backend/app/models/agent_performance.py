from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentPerformance(Base):
    __tablename__ = "agent_performance"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_date: Mapped[date] = mapped_column(Date, server_default=func.current_date(), nullable=False)

    starting_capital: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    ending_capital: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    daily_pnl: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    daily_pnl_pct: Mapped[float] = mapped_column(Numeric(10, 4), default=0)

    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, default=0)
    max_drawdown: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    sharpe_ratio: Mapped[float | None] = mapped_column(Numeric(10, 4))
    win_rate: Mapped[float] = mapped_column(Numeric(6, 3), default=0)
    avg_win: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    avg_loss: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    strategy_params_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("agent_id", "snapshot_date", name="uq_agent_performance_day"),)
