from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class NotificationPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: Literal[
        "trade_opened",
        "trade_closed",
        "risk_alert",
        "agent_stopped",
        "profit_target",
        "system",
    ]
    title: str
    message: str
    data: dict[str, Any]
    read: bool
    created_at: datetime
