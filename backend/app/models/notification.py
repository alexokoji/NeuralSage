from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from beanie import Document, Indexed
from pydantic import Field


class Notification(Document):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    user_id: Annotated[uuid.UUID, Indexed()]
    type: str
    title: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    read: bool = False
    created_at: Annotated[datetime, Indexed()] = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "notifications"
