"""Micro-scalping: tiny profit captures off short-window mean reversion.

Designed for 1m–5m timeframes. Uses a small EMA + Bollinger-style band
to enter on extreme deviations and exit on snap-back.

Trend filter: uses a longer EMA (20-period by default) to detect trend
direction. Mean-reversion entries are only allowed when the trend is FLAT
or moving against the proposed entry direction. This prevents buying dips
in a downtrend and shorting spikes in an uptrend — the root cause of
repeated losses when the market is clearly trending.
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
        # Trend filter: EMA slope over `trend_period` bars must be below
        # `trend_slope_threshold` (in % per bar) to allow entry.
        # A steep downward slope blocks longs; a steep upward slope blocks shorts.
        "trend_period": 20,
        "trend_slope_threshold": 0.03,  # % per bar — above this = strong trend, block entry
    }

    def evaluate(self, candles: pd.DataFrame, params: dict[str, Any], ctx: StrategyContext) -> Signal:
        params = self.merge_params(params)
        period = int(params["ema_period"])
        trend_period = int(params.get("trend_period", 20))
        trend_slope_threshold = float(params.get("trend_slope_threshold", 0.03))
        min_bars = max(period + 3, trend_period + 3)

        if len(candles) < min_bars:
            return Signal("hold", 0.0, "insufficient candles")

        close = candles["close"]
        e = ema(close, period)
        last_close = float(close.iloc[-1])
        last_ema = float(e.iloc[-1])
        deviation = (last_close - last_ema) / last_ema * 100  # in %

        # Trend direction: slope of the longer EMA over the last trend_period bars.
        # Positive slope = uptrend; negative slope = downtrend.
        trend_ema = ema(close, trend_period)
        trend_slope = (float(trend_ema.iloc[-1]) - float(trend_ema.iloc[-trend_period])) / float(trend_ema.iloc[-trend_period]) * 100

        meta = {
            "ema": last_ema,
            "last_close": last_close,
            "deviation_pct": deviation,
            "trend_slope_pct": round(trend_slope, 4),
        }

        if ctx.in_position:
            if ctx.position_side == "long" and deviation >= 0:
                return Signal("exit", 0.7, "reverted to EMA from below", metadata=meta)
            if ctx.position_side == "short" and deviation <= 0:
                return Signal("exit", 0.7, "reverted to EMA from above", metadata=meta)
            return Signal("hold", 0.5, "scalp running", metadata=meta)

        threshold = float(params["deviation_pct"])
        min_conf = float(params.get("min_confidence", 0.50))

        # Trend filter: block entries that fight a strong trend.
        # In a strong downtrend (slope < -threshold) a long is fading the trend.
        # In a strong uptrend (slope > +threshold) a short is fading the trend.
        long_trend_blocked = trend_slope < -trend_slope_threshold
        short_trend_blocked = trend_slope > trend_slope_threshold

        # Extreme dip → fade to long (only if not in a strong downtrend)
        if deviation <= -threshold:
            strength = abs(deviation) / threshold
            conf = min(0.85, 0.55 + strength * 0.15)
            if long_trend_blocked:
                # Downtrend: reduce confidence sharply — signal may be a continuation not a reversion
                conf = conf * 0.5
                meta["trend_filter"] = f"downtrend blocked long (slope={trend_slope:.3f}%)"
            if conf >= min_conf:
                reason = (
                    f"scalp: {deviation:.3f}% below EMA (threshold {threshold}%)"
                    + (f" [trend slope {trend_slope:.3f}%]" if long_trend_blocked else "")
                )
                return Signal(
                    "enter_long",
                    conf,
                    reason,
                    suggested_stop_loss_pct=params["stop_loss_pct"],
                    suggested_take_profit_pct=params["profit_target_pct"],
                    metadata=meta,
                )

        # Extreme spike → fade to short (only if not in a strong uptrend)
        if deviation >= threshold:
            strength = abs(deviation) / threshold
            conf = min(0.85, 0.55 + strength * 0.15)
            if short_trend_blocked:
                conf = conf * 0.5
                meta["trend_filter"] = f"uptrend blocked short (slope={trend_slope:.3f}%)"
            if conf >= min_conf:
                reason = (
                    f"scalp: {deviation:.3f}% above EMA (threshold {threshold}%)"
                    + (f" [trend slope {trend_slope:.3f}%]" if short_trend_blocked else "")
                )
                return Signal(
                    "enter_short",
                    conf,
                    reason,
                    suggested_stop_loss_pct=params["stop_loss_pct"],
                    suggested_take_profit_pct=params["profit_target_pct"],
                    metadata=meta,
                )

        proximity = abs(deviation) / max(threshold, 0.01)
        hold_conf = min(0.50, 0.35 + proximity * 0.15)
        return Signal("hold", hold_conf, f"deviation {deviation:.3f}% (threshold {threshold}%)", metadata=meta)
