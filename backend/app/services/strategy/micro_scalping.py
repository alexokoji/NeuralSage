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
        "ema_period": 5,
        "deviation_pct": 0.06,
        "profit_target_pct": 0.30,   # 0.30% covers Bitget's ~0.12% round-trip fee with margin
        "stop_loss_pct": 0.10,
        "max_trades_per_hour": 15,
        "position_size_pct": 1.5,
        "min_confidence": 0.45,
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
        min_conf = float(params.get("min_confidence", 0.50))

        # Extreme dip → fade to long
        if deviation <= -threshold:
            strength = abs(deviation) / threshold
            conf = min(0.85, 0.55 + strength * 0.15)
            if conf >= min_conf:
                return Signal(
                    "enter_long",
                    conf,
                    f"scalp: {deviation:.3f}% below EMA (threshold {threshold}%)",
                    suggested_stop_loss_pct=params["stop_loss_pct"],
                    suggested_take_profit_pct=params["profit_target_pct"],
                    metadata=meta,
                )
        # Extreme spike → fade to short
        if deviation >= threshold:
            strength = abs(deviation) / threshold
            conf = min(0.85, 0.55 + strength * 0.15)
            if conf >= min_conf:
                return Signal(
                    "enter_short",
                    conf,
                    f"scalp: {deviation:.3f}% above EMA (threshold {threshold}%)",
                    suggested_stop_loss_pct=params["stop_loss_pct"],
                    suggested_take_profit_pct=params["profit_target_pct"],
                    metadata=meta,
                )
        # Scale hold confidence by how close we are to the threshold
        # so the AI gets called on near-miss situations
        proximity = abs(deviation) / max(threshold, 0.01)
        hold_conf = min(0.50, 0.35 + proximity * 0.15)
        return Signal("hold", hold_conf, f"deviation {deviation:.3f}% (threshold {threshold}%)", metadata=meta)
