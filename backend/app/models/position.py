from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="CASCADE")
    )

    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)

    quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    entry_price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    current_price: Mapped[float | None] = mapped_column(Numeric(20, 8))
    stop_loss: Mapped[float | None] = mapped_column(Numeric(20, 8))
    take_profit: Mapped[float | None] = mapped_column(Numeric(20, 8))

    unrealized_pnl: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    unrealized_pnl_pct: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    leverage: Mapped[float] = mapped_column(Numeric(8, 2), default=1)
    margin_used: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user = relationship("User", back_populates="positions")

    __table_args__ = (
        CheckConstraint("side IN ('long','short')", name="ck_position_side"),
        Index("idx_positions_open", "is_open"),
    )
