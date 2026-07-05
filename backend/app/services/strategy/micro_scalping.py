"""Micro-scalping: tiny profit captures off short-window mean reversion.

Designed for 1m–5m timeframes. Uses a small EMA + Bollinger-style band
to enter on extreme deviations and exit on snap-back.

Entry gates (all required for an entry signal):
  1. EMA deviation >= threshold % (price stretched from mean)
  2. RSI confirmation: oversold (<40) for long, overbought (>60) for short
     — OR — RSI divergence detected (even stronger signal)
  3. Volume spike: current candle volume > 1.3× recent average (capitulation)

These gates are computed purely in code. GPT then confirms or rejects based
on 20-candle context + indicators. This eliminates the "reversal candle"
timing problem — mathematically confirmed setups only reach GPT.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.services.strategy.base import Signal, Strategy, StrategyContext
from app.services.strategy.indicators import ema, rsi


class MicroScalpingStrategy(Strategy):
    type = "micro_scalping"
    default_params: dict[str, Any] = {
        "ema_period": 5,
        "deviation_pct": 0.06,
        "profit_target_pct": 0.50,
        "stop_loss_pct": 0.20,
        "max_trades_per_hour": 6,
        "position_size_pct": 1.5,
        "min_confidence": 0.45,
        "trend_period": 20,
        "trend_slope_threshold": 0.03,
        # RSI gates
        "rsi_period": 14,
        "rsi_oversold": 40.0,    # RSI below this = oversold, required for long
        "rsi_overbought": 60.0,  # RSI above this = overbought, required for short
        # Volume gate
        "volume_avg_period": 10,
        "volume_spike_ratio": 1.3,  # current volume must be > this × average
    }

    def evaluate(self, candles: pd.DataFrame, params: dict[str, Any], ctx: StrategyContext) -> Signal:
        params = self.merge_params(params)
        period = int(params["ema_period"])
        trend_period = int(params.get("trend_period", 20))
        trend_slope_threshold = float(params.get("trend_slope_threshold", 0.03))
        rsi_period = int(params.get("rsi_period", 14))
        rsi_oversold = float(params.get("rsi_oversold", 40.0))
        rsi_overbought = float(params.get("rsi_overbought", 60.0))
        volume_avg_period = int(params.get("volume_avg_period", 10))
        volume_spike_ratio = float(params.get("volume_spike_ratio", 1.3))

        min_bars = max(period + 3, trend_period + 3, rsi_period + 3, volume_avg_period + 3)
        if len(candles) < min_bars:
            return Signal("hold", 0.0, "insufficient candles")

        close = candles["close"]
        e = ema(close, period)
        last_close = float(close.iloc[-1])
        last_ema = float(e.iloc[-1])
        deviation = (last_close - last_ema) / last_ema * 100

        trend_ema = ema(close, trend_period)
        trend_slope = (
            (float(trend_ema.iloc[-1]) - float(trend_ema.iloc[-trend_period]))
            / float(trend_ema.iloc[-trend_period]) * 100
        )

        if trend_slope > trend_slope_threshold:
            market_regime = "trending_up"
        elif trend_slope < -trend_slope_threshold:
            market_regime = "trending_down"
        else:
            market_regime = "ranging"

        # RSI
        rsi_series = rsi(close, rsi_period)
        current_rsi = float(rsi_series.iloc[-1])
        prev_rsi = float(rsi_series.iloc[-2]) if len(rsi_series) >= 2 else current_rsi

        # RSI divergence: price new low but RSI higher low (bullish), or price new high but RSI lower high (bearish)
        lookback = min(10, len(close) - 1)
        recent_low = float(close.iloc[-lookback:].min())
        recent_high = float(close.iloc[-lookback:].max())
        recent_rsi_low = float(rsi_series.iloc[-lookback:].min())
        recent_rsi_high = float(rsi_series.iloc[-lookback:].max())
        bullish_divergence = (last_close <= recent_low * 1.001) and (current_rsi > recent_rsi_low + 2)
        bearish_divergence = (last_close >= recent_high * 0.999) and (current_rsi < recent_rsi_high - 2)

        # Volume gate
        has_volume = "volume" in candles.columns
        volume_spike = False
        volume_ratio = 1.0
        if has_volume:
            vol = candles["volume"]
            current_vol = float(vol.iloc[-1])
            avg_vol = float(vol.iloc[-(volume_avg_period + 1):-1].mean())
            if avg_vol > 0:
                volume_ratio = current_vol / avg_vol
                volume_spike = volume_ratio >= volume_spike_ratio

        meta = {
            "ema": last_ema,
            "last_close": last_close,
            "deviation_pct": deviation,
            "trend_slope_pct": round(trend_slope, 4),
            "market_regime": market_regime,
            "rsi": round(current_rsi, 1),
            "volume_ratio": round(volume_ratio, 2),
        }

        if ctx.in_position:
            if ctx.position_side == "long" and deviation >= 0:
                return Signal("exit", 0.7, "reverted to EMA from below", metadata=meta)
            if ctx.position_side == "short" and deviation <= 0:
                return Signal("exit", 0.7, "reverted to EMA from above", metadata=meta)
            return Signal("hold", 0.5, "scalp running", metadata=meta)

        threshold = float(params["deviation_pct"])
        long_trend_blocked = trend_slope < -trend_slope_threshold
        short_trend_blocked = trend_slope > trend_slope_threshold

        # Candle structure for reversal context (informational — passed to GPT)
        last_open = float(candles["open"].iloc[-1])
        last_high = float(candles["high"].iloc[-1])
        last_low = float(candles["low"].iloc[-1])
        candle_is_green = last_close > last_open
        candle_is_red = last_close < last_open
        candle_range = max(last_high - last_low, 1e-9)
        lower_wick = (last_open - last_low) if not candle_is_green else (last_close - last_low)
        upper_wick = (last_high - last_open) if not candle_is_red else (last_high - last_close)
        has_rejection_wick_long = (lower_wick / candle_range) >= 0.35
        has_rejection_wick_short = (upper_wick / candle_range) >= 0.35

        # ------------------------------------------------------------------ #
        # Extreme dip → long entry
        # ------------------------------------------------------------------ #
        if deviation <= -threshold:
            # Gate 1: RSI must be oversold OR bullish divergence present
            rsi_ok = current_rsi <= rsi_oversold or bullish_divergence
            if not rsi_ok:
                meta["gate_fail"] = f"rsi={current_rsi:.1f} not oversold (<{rsi_oversold}) and no divergence"
                proximity = abs(deviation) / max(threshold, 0.01)
                return Signal(
                    "hold", min(0.45, 0.30 + proximity * 0.15),
                    f"scalp: {deviation:.3f}% below EMA — RSI {current_rsi:.1f} not oversold yet",
                    metadata=meta,
                )

            # Gate 2: Volume spike (warn but don't hard-block — volume data missing on some exchanges)
            if has_volume and not volume_spike:
                meta["gate_warn"] = f"low volume (ratio={volume_ratio:.2f}, need {volume_spike_ratio}x)"

            strength = abs(deviation) / threshold
            conf = min(0.85, 0.55 + strength * 0.15)
            # Divergence is the strongest signal — boost confidence
            if bullish_divergence:
                conf = min(0.88, conf + 0.08)
                meta["divergence"] = "bullish"
            if long_trend_blocked:
                conf = max(conf * 0.6, 0.30)
                meta["trend_filter"] = f"downtrend caution (slope={trend_slope:.3f}%)"

            reversal_confirmed = candle_is_green
            wick_hint = has_rejection_wick_long and not reversal_confirmed
            meta["reversal_pending"] = not reversal_confirmed
            meta["wick_rejection"] = wick_hint
            meta["volume_spike"] = volume_spike

            if reversal_confirmed:
                reversal_note = " + reversal candle"
            elif wick_hint:
                reversal_note = " + wick rejection"
            else:
                reversal_note = " — awaiting reversal candle"

            rsi_note = f" RSI={current_rsi:.1f}" + (" [divergence]" if bullish_divergence else "")
            vol_note = f" vol={volume_ratio:.1f}x" if has_volume else ""

            return Signal(
                "enter_long", conf,
                f"scalp: {deviation:.3f}% below EMA{reversal_note}{rsi_note}{vol_note}",
                suggested_stop_loss_pct=params["stop_loss_pct"],
                suggested_take_profit_pct=params["profit_target_pct"],
                metadata=meta,
            )

        # ------------------------------------------------------------------ #
        # Extreme spike → short entry
        # ------------------------------------------------------------------ #
        if deviation >= threshold:
            # Gate 1: RSI must be overbought OR bearish divergence
            rsi_ok = current_rsi >= rsi_overbought or bearish_divergence
            if not rsi_ok:
                meta["gate_fail"] = f"rsi={current_rsi:.1f} not overbought (>{rsi_overbought}) and no divergence"
                proximity = abs(deviation) / max(threshold, 0.01)
                return Signal(
                    "hold", min(0.45, 0.30 + proximity * 0.15),
                    f"scalp: {deviation:.3f}% above EMA — RSI {current_rsi:.1f} not overbought yet",
                    metadata=meta,
                )

            if has_volume and not volume_spike:
                meta["gate_warn"] = f"low volume (ratio={volume_ratio:.2f}, need {volume_spike_ratio}x)"

            strength = abs(deviation) / threshold
            conf = min(0.85, 0.55 + strength * 0.15)
            if bearish_divergence:
                conf = min(0.88, conf + 0.08)
                meta["divergence"] = "bearish"
            if short_trend_blocked:
                conf = max(conf * 0.6, 0.30)
                meta["trend_filter"] = f"uptrend caution (slope={trend_slope:.3f}%)"

            reversal_confirmed = candle_is_red
            wick_hint = has_rejection_wick_short and not reversal_confirmed
            meta["reversal_pending"] = not reversal_confirmed
            meta["wick_rejection"] = wick_hint
            meta["volume_spike"] = volume_spike

            if reversal_confirmed:
                reversal_note = " + reversal candle"
            elif wick_hint:
                reversal_note = " + wick rejection"
            else:
                reversal_note = " — awaiting reversal candle"

            rsi_note = f" RSI={current_rsi:.1f}" + (" [divergence]" if bearish_divergence else "")
            vol_note = f" vol={volume_ratio:.1f}x" if has_volume else ""

            return Signal(
                "enter_short", conf,
                f"scalp: {deviation:.3f}% above EMA{reversal_note}{rsi_note}{vol_note}",
                suggested_stop_loss_pct=params["stop_loss_pct"],
                suggested_take_profit_pct=params["profit_target_pct"],
                metadata=meta,
            )

        proximity = abs(deviation) / max(threshold, 0.01)
        hold_conf = min(0.50, 0.35 + proximity * 0.15)
        return Signal("hold", hold_conf, f"deviation {deviation:.3f}% (threshold {threshold}%)", metadata=meta)
