"""Strategy abstraction.

Strategies are PURE: given (candles, params) they emit a signal.
They never touch exchanges, the DB, or risk decisions — that is the
trading engine's job. This keeps strategies testable and replaceable.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd

SignalAction = Literal["enter_long", "enter_short", "exit", "hold"]


@dataclass
class StrategyContext:
    symbol: str
    timeframe: str
    in_position: bool = False
    position_side: Literal["long", "short"] | None = None


@dataclass
class Signal:
    action: SignalAction
    confidence: float  # 0..1
    reason: str = ""
    suggested_stop_loss_pct: float | None = None
    suggested_take_profit_pct: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    """Base class for all trading strategies."""

    type: str
    default_params: dict[str, Any] = {}

    @abstractmethod
    def evaluate(
        self,
        candles: pd.DataFrame,
        params: dict[str, Any],
        ctx: StrategyContext,
    ) -> Signal: ...

    def merge_params(self, override: dict[str, Any] | None) -> dict[str, Any]:
        merged = dict(self.default_params)
        if override:
            merged.update({k: v for k, v in override.items() if v is not None})
        return merged
