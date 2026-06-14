from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class BalanceEntry(BaseModel):
    asset: str
    available: float
    total: float
    usd_value: float | None = None


class ExchangeBalance(BaseModel):
    api_key_id: str
    exchange: str
    is_testnet: bool
    balances: list[BalanceEntry]
    updated_at: str
    error: str | None = None  # set when balance fetch fails


class PortfolioOverview(BaseModel):
    total_balance_usd: float
    total_pnl: float
    total_pnl_pct: float
    daily_pnl: float
    daily_pnl_pct: float
    open_positions_count: int
    active_agents_count: int
    exchanges: list[ExchangeBalance]


class CandlePoint(BaseModel):
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


class MarketTicker(BaseModel):
    symbol: str
    price: float
    change_24h_pct: float
    volume_24h: float
    high_24h: float
    low_24h: float


class StrategyBacktestPoint(BaseModel):
    timestamp: int
    equity: float
    drawdown: float
    metadata: dict[str, Any] | None = None
