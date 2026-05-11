"""RSI mean-reversion entries with confirmation."""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.services.strategy.base import Signal, Strategy, StrategyContext
from app.services.strategy.indicators import ema, rsi


class RSIEntryStrategy(Strategy):
    type = "rsi_entry"
    default_params: dict[str, Any] = {
        "rsi_period": 14,
        "oversold": 30,
        "overbought": 70,
        "trend_ema": 50,
        "stop_loss_pct": 1.0,
        "take_profit_pct": 2.0,
        "position_size_pct": 3.0,
        "min_confidence": 0.5,
    }

    def evaluate(self, candles: pd.DataFrame, params: dict[str, Any], ctx: StrategyContext) -> Signal:
        params = self.merge_params(params)
        period = int(params["rsi_period"])
        if len(candles) < max(period, int(params["trend_ema"])) + 3:
            return Signal("hold", 0.0, "insufficient candles")

        rsi_series = rsi(candles["close"], period)
        trend = ema(candles["close"], int(params["trend_ema"]))

        last_rsi = float(rsi_series.iloc[-1])
        prev_rsi = float(rsi_series.iloc[-2])
        last_close = float(candles["close"].iloc[-1])
        last_trend = float(trend.iloc[-1])

        meta = {"rsi": last_rsi, "trend_ema": last_trend, "last_close": last_close}

        if not ctx.in_position:
            # Buy oversold rebound, but only if price is at/above the trend filter.
            if (
                prev_rsi < params["oversold"]
                and last_rsi > params["oversold"]
                and last_close >= last_trend * 0.99
            ):
                conf = min(1.0, 0.5 + (params["oversold"] - min(prev_rsi, last_rsi)) / 100)
                if conf >= params["min_confidence"]:
                    return Signal(
                        "enter_long",
                        conf,
                        "RSI exited oversold",
                        suggested_stop_loss_pct=params["stop_loss_pct"],
                        suggested_take_profit_pct=params["take_profit_pct"],
                        metadata=meta,
                    )
            if (
                prev_rsi > params["overbought"]
                and last_rsi < params["overbought"]
                and last_close <= last_trend * 1.01
            ):
                conf = min(1.0, 0.5 + (max(prev_rsi, last_rsi) - params["overbought"]) / 100)
                if conf >= params["min_confidence"]:
                    return Signal(
                        "enter_short",
                        conf,
                        "RSI exited overbought",
                        suggested_stop_loss_pct=params["stop_loss_pct"],
                        suggested_take_profit_pct=params["take_profit_pct"],
                        metadata=meta,
                    )
            return Signal("hold", 0.4, "no RSI extreme", metadata=meta)

        # Exit: long once RSI reaches mid/upper, short once RSI hits mid/lower.
        if ctx.position_side == "long" and last_rsi >= 60:
            return Signal("exit", 0.7, "RSI back to neutral/upper", metadata=meta)
        if ctx.position_side == "short" and last_rsi <= 40:
            return Signal("exit", 0.7, "RSI back to neutral/lower", metadata=meta)
        return Signal("hold", 0.5, "in position", metadata=meta)
