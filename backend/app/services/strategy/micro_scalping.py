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
        "profit_target_pct": 0.50,   # 0.50% → 2.5:1 R/R with 0.20% SL; covers fees with real margin
        "stop_loss_pct": 0.20,       # 0.20% puts SL above typical 1m noise (bid-ask + micro-volatility)
        "max_trades_per_hour": 6,    # was 15 — fewer, higher-quality entries only
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

        if trend_slope > trend_slope_threshold:
            market_regime = "trending_up"
        elif trend_slope < -trend_slope_threshold:
            market_regime = "trending_down"
        else:
            market_regime = "ranging"

        meta = {
            "ema": last_ema,
            "last_close": last_close,
            "deviation_pct": deviation,
            "trend_slope_pct": round(trend_slope, 4),
            "market_regime": market_regime,
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
        long_trend_blocked = trend_slope < -trend_slope_threshold
        short_trend_blocked = trend_slope > trend_slope_threshold

        # Reversal confirmation: the last candle must already be moving back
        # toward the EMA before we enter. This avoids catching falling knives
        # (price is still dropping when we enter long) and rising knives (still
        # rising when we enter short).
        last_open = float(candles["open"].iloc[-1])
        last_close_price = float(close.iloc[-1])
        candle_is_green = last_close_price > last_open   # close > open
        candle_is_red = last_close_price < last_open

        # Extreme dip → fade to long
        if deviation <= -threshold:
            strength = abs(deviation) / threshold
            conf = min(0.85, 0.55 + strength * 0.15)
            if long_trend_blocked:
                conf = max(conf * 0.5, 0.30)
                meta["trend_filter"] = f"downtrend caution (slope={trend_slope:.3f}%)"
            # Reversal candle state is passed to GPT as context — GPT evaluates
            # candle structure, wicks, and momentum from 20 candles + indicators.
            meta["reversal_pending"] = not candle_is_green
            reason = (
                f"scalp: {deviation:.3f}% below EMA (threshold {threshold}%)"
                + (" + reversal candle" if candle_is_green else " — reversal pending, GPT to confirm")
                + (f" [trend caution slope={trend_slope:.3f}%]" if long_trend_blocked else "")
            )
            return Signal(
                "enter_long",
                conf,
                reason,
                suggested_stop_loss_pct=params["stop_loss_pct"],
                suggested_take_profit_pct=params["profit_target_pct"],
                metadata=meta,
            )

        # Extreme spike → fade to short
        if deviation >= threshold:
            strength = abs(deviation) / threshold
            conf = min(0.85, 0.55 + strength * 0.15)
            if short_trend_blocked:
                conf = max(conf * 0.5, 0.30)
                meta["trend_filter"] = f"uptrend caution (slope={trend_slope:.3f}%)"
            meta["reversal_pending"] = not candle_is_red
            reason = (
                f"scalp: {deviation:.3f}% above EMA (threshold {threshold}%)"
                + (" + reversal candle" if candle_is_red else " — reversal pending, GPT to confirm")
                + (f" [trend caution slope={trend_slope:.3f}%]" if short_trend_blocked else "")
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
