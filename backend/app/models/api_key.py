from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="")

    # AES-256-GCM ciphertext + nonce, stored base64-encoded.
    encrypted_api_key: Mapped[str] = mapped_column(String, nullable=False)
    encrypted_api_secret: Mapped[str] = mapped_column(String, nullable=False)
    encryption_iv: Mapped[str] = mapped_column(String, nullable=False)

    permissions: Mapped[list[str]] = mapped_column(
        ARRAY(String), server_default=text("ARRAY['read','trade']::varchar[]")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_testnet: Mapped[bool] = mapped_column(Boolean, default=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    balance_cache: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    balance_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user = relationship("User", back_populates="api_keys")

    __table_args__ = (
        CheckConstraint("exchange IN ('bybit','bitget','bybit_testnet')", name="ck_api_keys_exchange"),
        CheckConstraint("NOT ('withdraw' = ANY(permissions))", name="ck_api_keys_no_withdraw"),
    )
