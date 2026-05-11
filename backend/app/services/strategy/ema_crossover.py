"""Trend-following EMA crossover.

Long  when fast EMA crosses above slow EMA.
Short when fast EMA crosses below slow EMA.
Exit when the cross reverses.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.services.strategy.base import Signal, Strategy, StrategyContext
from app.services.strategy.indicators import ema


class EMACrossoverStrategy(Strategy):
    type = "ema_crossover"
    default_params: dict[str, Any] = {
        "fast_ema": 9,
        "slow_ema": 21,
        "stop_loss_pct": 1.5,
        "take_profit_pct": 3.0,
        "position_size_pct": 5.0,
        "min_confidence": 0.55,
    }

    def evaluate(self, candles: pd.DataFrame, params: dict[str, Any], ctx: StrategyContext) -> Signal:
        params = self.merge_params(params)
        if len(candles) < int(params["slow_ema"]) + 5:
            return Signal("hold", 0.0, "insufficient candles")

        close = candles["close"]
        fast = ema(close, int(params["fast_ema"]))
        slow = ema(close, int(params["slow_ema"]))

        prev_diff = fast.iloc[-2] - slow.iloc[-2]
        cur_diff = fast.iloc[-1] - slow.iloc[-1]
        spread_pct = abs(cur_diff) / float(close.iloc[-1])
        confidence = min(1.0, 0.4 + spread_pct * 50)

        meta = {
            "fast_ema": float(fast.iloc[-1]),
            "slow_ema": float(slow.iloc[-1]),
            "spread_pct": float(spread_pct * 100),
        }

        if not ctx.in_position:
            if prev_diff <= 0 and cur_diff > 0 and confidence >= params["min_confidence"]:
                return Signal(
                    "enter_long",
                    confidence,
                    "fast EMA crossed above slow EMA",
                    suggested_stop_loss_pct=params["stop_loss_pct"],
                    suggested_take_profit_pct=params["take_profit_pct"],
                    metadata=meta,
                )
            if prev_diff >= 0 and cur_diff < 0 and confidence >= params["min_confidence"]:
                return Signal(
                    "enter_short",
                    confidence,
                    "fast EMA crossed below slow EMA",
                    suggested_stop_loss_pct=params["stop_loss_pct"],
                    suggested_take_profit_pct=params["take_profit_pct"],
                    metadata=meta,
                )
            return Signal("hold", confidence, "no crossover", metadata=meta)

        # In a position — check for reversal exit.
        if ctx.position_side == "long" and cur_diff < 0:
            return Signal("exit", confidence, "EMA crossed back down", metadata=meta)
        if ctx.position_side == "short" and cur_diff > 0:
            return Signal("exit", confidence, "EMA crossed back up", metadata=meta)
        return Signal("hold", confidence, "trend intact", metadata=meta)
