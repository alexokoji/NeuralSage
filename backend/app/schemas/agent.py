from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AgentStatus = Literal["idle", "active", "paused", "stopped", "error"]
StrategyType = str  # was Literal; now accepts composite + custom strategies
Timeframe = Literal["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"]


class StrategyPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    type: StrategyType
    description: str
    default_params: dict[str, Any]
    is_system: bool


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    api_key_id: uuid.UUID
    strategy_id: uuid.UUID
    assigned_capital: float = Field(ge=0)
    currency: str = Field(default="USDT", max_length=16)
    trading_pairs: list[str] = Field(default_factory=lambda: ["BTCUSDT"])
    timeframe: Timeframe = "15m"
    max_risk_per_trade: float = Field(default=2.0, ge=0.1, le=5.0)
    daily_profit_target: float = Field(default=3.0, ge=0)
    weekly_profit_target: float = Field(default=10.0, ge=0)
    max_daily_loss: float = Field(default=5.0, ge=0, le=50)
    max_concurrent_trades: int = Field(default=3, ge=1, le=20)
    max_consecutive_losses: int = Field(default=3, ge=1, le=10)
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    ai_optimization_enabled: bool = True
    is_paper_trade: bool = False


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    api_key_id: uuid.UUID | None = None
    assigned_capital: float | None = Field(default=None, ge=0)
    trading_pairs: list[str] | None = None
    timeframe: Timeframe | None = None
    max_risk_per_trade: float | None = Field(default=None, ge=0.1, le=5.0)
    daily_profit_target: float | None = Field(default=None, ge=0)
    weekly_profit_target: float | None = Field(default=None, ge=0)
    max_daily_loss: float | None = Field(default=None, ge=0, le=50)
    max_concurrent_trades: int | None = Field(default=None, ge=1, le=20)
    max_consecutive_losses: int | None = Field(default=None, ge=1, le=10)
    strategy_params: dict[str, Any] | None = None
    ai_optimization_enabled: bool | None = None
    is_paper_trade: bool | None = None


class AgentPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    api_key_id: uuid.UUID | None
    strategy_id: uuid.UUID | None
    name: str
    description: str
    status: AgentStatus
    assigned_capital: float
    currency: str
    trading_pairs: list[str]
    timeframe: Timeframe
    max_risk_per_trade: float
    daily_profit_target: float
    weekly_profit_target: float
    max_daily_loss: float
    max_concurrent_trades: int
    max_consecutive_losses: int
    strategy_params: dict[str, Any]
    ai_optimization_enabled: bool
    is_paper_trade: bool
    optimization_params: dict[str, Any]
    total_pnl: float
    total_trades: int
    winning_trades: int
    current_day_pnl: float
    current_week_pnl: float
    confidence_score: float
    last_trade_at: datetime | None
    started_at: datetime | None
    created_at: datetime
    strategy: StrategyPublic | None = None
    last_tick_at: datetime | None = None
    last_signal: str | None = None
    last_signal_symbol: str | None = None
    last_error: str | None = None
    tick_count: int = 0
    winding_down: bool = False
    cooldown_until: datetime | None = None
    session_trade_count: int = 0
    trades_per_session: int = 10
    protect_mode: bool = False
    profit_protect_pct: float = 15.0
    recovery_mode: bool = False


class AgentControlAction(BaseModel):
    action: Literal["start", "pause", "stop"]
