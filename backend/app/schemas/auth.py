from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(default="", max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    username: str | None
    avatar_url: str
    timezone: str
    risk_level: Literal["low", "medium", "high"]
    daily_loss_limit: float
    max_concurrent_trades: int
    notifications_enabled: bool
    two_factor_enabled: bool
    created_at: datetime


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=120)
    username: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=64)
    risk_level: Literal["low", "medium", "high"] | None = None
    daily_loss_limit: float | None = Field(default=None, ge=0, le=50)
    max_concurrent_trades: int | None = Field(default=None, ge=1, le=20)
    notifications_enabled: bool | None = None
