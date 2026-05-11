"""Tiny dependency-free technical indicators.

We avoid pulling TA-Lib or `ta` here for hot-path determinism — these formulas
are textbook and trivially testable.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def rolling_high(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=1).max()


def rolling_low(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=1).min()


def candles_to_df(candles: list) -> pd.DataFrame:
    """Accepts list of Candle dataclasses or dicts."""
    rows = []
    for c in candles:
        if hasattr(c, "open_time"):
            rows.append((c.open_time, c.open, c.high, c.low, c.close, c.volume))
        else:
            rows.append((c["t"], c["o"], c["h"], c["l"], c["c"], c["v"]))
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    return df.sort_values("time").reset_index(drop=True)
