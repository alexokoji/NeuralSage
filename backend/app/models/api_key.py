from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from beanie import Document, Indexed
from pydantic import Field


class ApiKey(Document):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    user_id: Annotated[uuid.UUID, Indexed()]
    exchange: str
    label: str = ""

    encrypted_api_key: str
    encrypted_api_secret: str
    encryption_iv: str

    permissions: list[str] = Field(default_factory=lambda: ["read", "trade"])
    is_active: bool = True
    is_testnet: bool = False
    verified: bool = False
    last_verified_at: Optional[datetime] = None

    balance_cache: dict[str, Any] = Field(default_factory=dict)
    balance_updated_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "api_keys"
