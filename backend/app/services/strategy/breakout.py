"""Donchian-style breakout from N-bar consolidation, volume-confirmed."""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.services.strategy.base import Signal, Strategy, StrategyContext
from app.services.strategy.indicators import atr, rolling_high, rolling_low


class BreakoutStrategy(Strategy):
    type = "breakout"
    default_params: dict[str, Any] = {
        "lookback_period": 20,
        "breakout_threshold_pct": 0.5,  # min move past prior high/low, in %
        "volume_multiplier": 1.5,
        "stop_loss_pct": 1.0,
        "take_profit_pct": 2.5,
        "position_size_pct": 3.0,
        "min_confidence": 0.6,
    }

    def evaluate(self, candles: pd.DataFrame, params: dict[str, Any], ctx: StrategyContext) -> Signal:
        params = self.merge_params(params)
        lookback = int(params["lookback_period"])
        if len(candles) < lookback + 3:
            return Signal("hold", 0.0, "insufficient candles")

        prev_window = candles.iloc[-(lookback + 1) : -1]
        last = candles.iloc[-1]

        prior_high = float(rolling_high(prev_window["high"], lookback).iloc[-1])
        prior_low = float(rolling_low(prev_window["low"], lookback).iloc[-1])
        avg_volume = float(prev_window["volume"].mean())

        threshold = float(params["breakout_threshold_pct"]) / 100
        last_close = float(last["close"])
        last_volume = float(last["volume"])

        meta = {
            "prior_high": prior_high,
            "prior_low": prior_low,
            "avg_volume": avg_volume,
            "last_close": last_close,
            "last_volume": last_volume,
            "atr": float(atr(candles).iloc[-1]),
        }

        if ctx.in_position:
            # Trail logic: exit if back inside the prior range.
            if ctx.position_side == "long" and last_close < prior_high * (1 - threshold):
                return Signal("exit", 0.7, "breakout failed (long)", metadata=meta)
            if ctx.position_side == "short" and last_close > prior_low * (1 + threshold):
                return Signal("exit", 0.7, "breakout failed (short)", metadata=meta)
            return Signal("hold", 0.6, "breakout extending", metadata=meta)

        volume_ok = last_volume >= avg_volume * float(params["volume_multiplier"])
        if last_close > prior_high * (1 + threshold) and volume_ok:
            spread = (last_close - prior_high) / prior_high
            conf = min(1.0, 0.5 + spread * 80)
            if conf >= params["min_confidence"]:
                return Signal(
                    "enter_long",
                    conf,
                    "upside breakout with volume",
                    suggested_stop_loss_pct=params["stop_loss_pct"],
                    suggested_take_profit_pct=params["take_profit_pct"],
                    metadata=meta,
                )
        if last_close < prior_low * (1 - threshold) and volume_ok:
            spread = (prior_low - last_close) / prior_low
            conf = min(1.0, 0.5 + spread * 80)
            if conf >= params["min_confidence"]:
                return Signal(
                    "enter_short",
                    conf,
                    "downside breakout with volume",
                    suggested_stop_loss_pct=params["stop_loss_pct"],
                    suggested_take_profit_pct=params["take_profit_pct"],
                    metadata=meta,
                )
        return Signal("hold", 0.4, "no breakout", metadata=meta)
