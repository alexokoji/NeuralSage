from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ExchangeName = Literal["bybit", "bitget", "bybit_testnet", "deriv", "deriv_live"]
PermissionName = Literal["read", "trade"]


class ApiKeyCreate(BaseModel):
    exchange: ExchangeName
    label: str = Field(default="", max_length=120)
    api_key: str = Field(min_length=8, max_length=4096)
    api_secret: str = Field(min_length=8, max_length=512)
    is_testnet: bool = False
    permissions: list[PermissionName] = Field(default_factory=lambda: ["read", "trade"])


class ApiKeyPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    exchange: ExchangeName
    label: str
    permissions: list[str]
    is_active: bool
    is_testnet: bool
    verified: bool
    last_verified_at: datetime | None
    balance_cache: dict
    balance_updated_at: datetime | None
    created_at: datetime


class ApiKeyVerifyResult(BaseModel):
    verified: bool
    permissions: list[str]
    error: str | None = None
    server_blocked: bool = False  # True when exchange returns 403 (cloud IP blocked by WAF)
