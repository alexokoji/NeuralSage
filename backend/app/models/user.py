from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Optional

from beanie import Document, Indexed
from pydantic import Field


class User(Document):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    email: Annotated[str, Indexed(unique=True)]
    hashed_password: str
    full_name: str = ""
    username: Annotated[Optional[str], Indexed(unique=True)] = None
    avatar_url: str = ""
    timezone: str = "UTC"
    risk_level: str = "medium"
    daily_loss_limit: float = 5.0
    max_concurrent_trades: int = 5
    notifications_enabled: bool = True
    two_factor_enabled: bool = False
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "users"
