from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from beanie import Document, Indexed
from pydantic import BaseModel, Field


class StrategyEmbed(BaseModel):
    """Snapshot of the strategy embedded in the agent document."""
    id: uuid.UUID
    name: str
    type: str
    description: str = ""
    default_params: dict[str, Any] = Field(default_factory=dict)
    is_system: bool = True


class Agent(Document):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    user_id: Annotated[uuid.UUID, Indexed()]
    api_key_id: Optional[uuid.UUID] = None
    strategy_id: Optional[uuid.UUID] = None
    strategy: Optional[StrategyEmbed] = None  # embedded for fast reads

    name: str
    description: str = ""
    status: Annotated[str, Indexed()] = "idle"

    assigned_capital: float = 0.0
    currency: str = "USDT"
    trading_pairs: list[str] = Field(default_factory=lambda: ["BTCUSDT"])
    timeframe: str = "15m"

    max_risk_per_trade: float = 2.0
    daily_profit_target: float = 3.0
    weekly_profit_target: float = 10.0
    max_daily_loss: float = 5.0
    max_concurrent_trades: int = 3
    max_consecutive_losses: int = 3

    strategy_params: dict[str, Any] = Field(default_factory=dict)
    ai_optimization_enabled: bool = True
    optimization_params: dict[str, Any] = Field(default_factory=dict)

    total_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    current_day_pnl: float = 0.0
    current_week_pnl: float = 0.0
    confidence_score: float = 50.0

    last_trade_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # When True, orders are simulated locally — no exchange API calls for entry/exit.
    is_paper_trade: bool = False

    # Recovery mode: set after emergency re-optimization. The risk engine
    # halves position size until a winning trade proves the new params work.
    recovery_mode: bool = False

    # 10-trade session cooldown: after placing this many trades, the agent
    # pauses for cooldown_hours to study fleet data before resuming.
    trades_per_session: int = 10
    cooldown_hours: float = 3.0
    session_trade_count: int = 0
    cooldown_until: Optional[datetime] = None

    # Profit protection: when total_pnl reaches this % of assigned_capital,
    # the agent switches to ultra-conservative mode (only very high confidence entries).
    profit_protect_pct: float = 15.0
    protect_mode: bool = False

    # Activity tracking — updated every scheduler tick so the UI can show live state
    last_tick_at: Optional[datetime] = None
    last_signal: Optional[str] = None          # "hold" | "enter_long" | "enter_short" | "exit"
    last_signal_symbol: Optional[str] = None
    last_error: Optional[str] = None
    tick_count: int = 0

    class Settings:
        name = "agents"
