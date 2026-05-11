"""Micro-scalping: tiny profit captures off short-window mean reversion.

Designed for 1m–5m timeframes. Uses a small EMA + Bollinger-style band
to enter on extreme deviations and exit on snap-back.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.services.strategy.base import Signal, Strategy, StrategyContext
from app.services.strategy.indicators import ema


class MicroScalpingStrategy(Strategy):
    type = "micro_scalping"
    default_params: dict[str, Any] = {
        "ema_period": 8,
        "deviation_pct": 0.25,
        "profit_target_pct": 0.3,
        "stop_loss_pct": 0.2,
        "max_trades_per_hour": 10,
        "position_size_pct": 2.0,
        "min_confidence": 0.5,
    }

    def evaluate(self, candles: pd.DataFrame, params: dict[str, Any], ctx: StrategyContext) -> Signal:
        params = self.merge_params(params)
        period = int(params["ema_period"])
        if len(candles) < period + 3:
            return Signal("hold", 0.0, "insufficient candles")

        close = candles["close"]
        e = ema(close, period)
        last_close = float(close.iloc[-1])
        last_ema = float(e.iloc[-1])
        deviation = (last_close - last_ema) / last_ema * 100  # in %

        meta = {"ema": last_ema, "last_close": last_close, "deviation_pct": deviation}

        if ctx.in_position:
            # Exit when price reverts close to ema, or stop/profit handled by engine.
            if ctx.position_side == "long" and deviation >= 0:
                return Signal("exit", 0.7, "reverted to EMA from below", metadata=meta)
            if ctx.position_side == "short" and deviation <= 0:
                return Signal("exit", 0.7, "reverted to EMA from above", metadata=meta)
            return Signal("hold", 0.5, "scalp running", metadata=meta)

        threshold = float(params["deviation_pct"])
        # Extreme dip → fade to long.
        if deviation <= -threshold:
            conf = min(1.0, 0.5 + abs(deviation) / (threshold * 4))
            if conf >= params["min_confidence"]:
                return Signal(
                    "enter_long",
                    conf,
                    "scalp: extended below EMA",
                    suggested_stop_loss_pct=params["stop_loss_pct"],
                    suggested_take_profit_pct=params["profit_target_pct"],
                    metadata=meta,
                )
        if deviation >= threshold:
            conf = min(1.0, 0.5 + abs(deviation) / (threshold * 4))
            if conf >= params["min_confidence"]:
                return Signal(
                    "enter_short",
                    conf,
                    "scalp: extended above EMA",
                    suggested_stop_loss_pct=params["stop_loss_pct"],
                    suggested_take_profit_pct=params["profit_target_pct"],
                    metadata=meta,
                )
        return Signal("hold", 0.4, "no extreme", metadata=meta)
