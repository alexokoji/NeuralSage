from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from beanie import Document, Indexed
from pydantic import Field


class Strategy(Document):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    name: str
    type: Annotated[str, Indexed(unique=True)]
    description: str = ""
    default_params: dict[str, Any] = Field(default_factory=dict)
    is_system: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "strategies"
