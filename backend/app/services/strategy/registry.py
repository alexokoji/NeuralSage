"""Strategy lookup by `type` string."""
from __future__ import annotations

from app.services.strategy.base import Strategy
from app.services.strategy.breakout import BreakoutStrategy
from app.services.strategy.composite import CompositeStrategy
from app.services.strategy.ema_crossover import EMACrossoverStrategy
from app.services.strategy.micro_scalping import MicroScalpingStrategy
from app.services.strategy.rsi_entry import RSIEntryStrategy

STRATEGIES: dict[str, Strategy] = {
    EMACrossoverStrategy.type: EMACrossoverStrategy(),
    RSIEntryStrategy.type: RSIEntryStrategy(),
    BreakoutStrategy.type: BreakoutStrategy(),
    MicroScalpingStrategy.type: MicroScalpingStrategy(),
    CompositeStrategy.type: CompositeStrategy(),
}


_FOREX_MAP = {
    "forex_ema_trend": "ema_crossover",
    "forex_rsi_reversal": "rsi_entry",
}


def get_strategy(strategy_type: str) -> Strategy:
    if strategy_type in STRATEGIES:
        return STRATEGIES[strategy_type]
    if strategy_type in _FOREX_MAP:
        return STRATEGIES[_FOREX_MAP[strategy_type]]
    if strategy_type.startswith("composite_"):
        return STRATEGIES["composite"]
    raise KeyError(f"unknown strategy: {strategy_type}")
