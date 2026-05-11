from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id", ondelete="SET NULL")
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String(16), default="idle", index=True)

    assigned_capital: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    currency: Mapped[str] = mapped_column(String(16), default="USDT")
    trading_pairs: Mapped[list[str]] = mapped_column(
        ARRAY(String), server_default=text("ARRAY['BTCUSDT']::varchar[]")
    )
    timeframe: Mapped[str] = mapped_column(String(8), default="15m")

    max_risk_per_trade: Mapped[float] = mapped_column(Numeric(6, 3), default=2.0)
    daily_profit_target: Mapped[float] = mapped_column(Numeric(8, 3), default=3.0)
    weekly_profit_target: Mapped[float] = mapped_column(Numeric(8, 3), default=10.0)
    max_daily_loss: Mapped[float] = mapped_column(Numeric(8, 3), default=5.0)
    max_concurrent_trades: Mapped[int] = mapped_column(Integer, default=3)
    max_consecutive_losses: Mapped[int] = mapped_column(Integer, default=3)

    strategy_params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    ai_optimization_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    optimization_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )

    total_pnl: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, default=0)
    current_day_pnl: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    current_week_pnl: Mapped[float] = mapped_column(Numeric(20, 8), default=0)
    confidence_score: Mapped[float] = mapped_column(Numeric(6, 3), default=50.0)

    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user = relationship("User", back_populates="agents")
    strategy = relationship("Strategy", lazy="joined")
    api_key = relationship("ApiKey", lazy="joined")

    __table_args__ = (
        CheckConstraint("status IN ('idle','active','paused','stopped','error')", name="ck_agent_status"),
        CheckConstraint("assigned_capital >= 0", name="ck_agent_capital_nonneg"),
        CheckConstraint("max_risk_per_trade <= 5.0", name="ck_agent_risk_cap"),
        CheckConstraint(
            "timeframe IN ('1m','3m','5m','15m','30m','1h','4h','1d')", name="ck_agent_timeframe"
        ),
        CheckConstraint("confidence_score BETWEEN 0 AND 100", name="ck_agent_confidence"),
    )
