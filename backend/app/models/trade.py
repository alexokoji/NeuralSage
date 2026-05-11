from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL")
    )

    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")

    quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    entry_price: Mapped[float | None] = mapped_column(Numeric(20, 8))
    exit_price: Mapped[float | None] = mapped_column(Numeric(20, 8))
    stop_loss: Mapped[float | None] = mapped_column(Numeric(20, 8))
    take_profit: Mapped[float | None] = mapped_column(Numeric(20, 8))

    pnl: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    pnl_pct: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    fees: Mapped[float] = mapped_column(Numeric(20, 8), default=0)

    signal_source: Mapped[str] = mapped_column(String(64), default="strategy")
    signal_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    risk_checks: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    notes: Mapped[str] = mapped_column(String, default="")

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="trades")
    agent = relationship("Agent", lazy="joined")

    __table_args__ = (
        CheckConstraint("side IN ('buy','sell')", name="ck_trade_side"),
        CheckConstraint(
            "order_type IN ('market','limit','stop_loss','take_profit')", name="ck_trade_order_type"
        ),
        CheckConstraint(
            "status IN ('pending','open','filled','cancelled','rejected','expired')",
            name="ck_trade_status",
        ),
        CheckConstraint("quantity > 0", name="ck_trade_qty_positive"),
        Index("idx_trades_created", "created_at"),
    )
