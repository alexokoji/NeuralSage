"""Lightweight backtester used by the AI optimizer.

Walks bar-by-bar, feeds the strategy a growing window, and simulates
flat-fee fills at the bar close. Computes a risk-adjusted score
(net return × win rate − drawdown penalty) used as the optimization
objective. Not a substitute for a proper engine — purpose-built for
parameter ranking on recent candles.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.services.strategy.base import Signal, Strategy, StrategyContext


@dataclass
class BacktestResult:
    trades: int
    wins: int
    net_return_pct: float
    win_rate: float
    max_drawdown_pct: float
    score: float


def backtest(
    strategy: Strategy,
    candles: pd.DataFrame,
    params: dict,
    *,
    fee_pct: float = 0.04,
    warmup: int = 50,
) -> BacktestResult:
    if len(candles) <= warmup + 5:
        return BacktestResult(0, 0, 0.0, 0.0, 0.0, -1.0)

    ctx = StrategyContext(symbol="BACKTEST", timeframe="?", in_position=False)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    entry_price = 0.0
    side: str | None = None
    sl_pct: float | None = None
    tp_pct: float | None = None
    trades = 0
    wins = 0

    for i in range(warmup, len(candles)):
        window = candles.iloc[: i + 1]
        sig: Signal = strategy.evaluate(window, params, ctx)
        last_close = float(window["close"].iloc[-1])
        last_high = float(window["high"].iloc[-1])
        last_low = float(window["low"].iloc[-1])

        # Stop / take-profit checks for an open position.
        if ctx.in_position and side and sl_pct is not None and tp_pct is not None:
            if side == "long":
                stop = entry_price * (1 - sl_pct / 100)
                target = entry_price * (1 + tp_pct / 100)
                if last_low <= stop:
                    ret = -sl_pct / 100 - fee_pct / 100
                    equity *= 1 + ret
                    trades += 1
                    ctx.in_position = False
                    side = None
                    continue
                if last_high >= target:
                    ret = tp_pct / 100 - fee_pct / 100
                    equity *= 1 + ret
                    trades += 1
                    wins += 1
                    ctx.in_position = False
                    side = None
                    continue
            else:  # short
                stop = entry_price * (1 + sl_pct / 100)
                target = entry_price * (1 - tp_pct / 100)
                if last_high >= stop:
                    ret = -sl_pct / 100 - fee_pct / 100
                    equity *= 1 + ret
                    trades += 1
                    ctx.in_position = False
                    side = None
                    continue
                if last_low <= target:
                    ret = tp_pct / 100 - fee_pct / 100
                    equity *= 1 + ret
                    trades += 1
                    wins += 1
                    ctx.in_position = False
                    side = None
                    continue

        # Apply fresh signal.
        if not ctx.in_position and sig.action in ("enter_long", "enter_short"):
            ctx.in_position = True
            side = "long" if sig.action == "enter_long" else "short"
            ctx.position_side = side  # type: ignore[assignment]
            entry_price = last_close
            sl_pct = sig.suggested_stop_loss_pct or 1.0
            tp_pct = sig.suggested_take_profit_pct or 2.0
        elif ctx.in_position and sig.action == "exit" and side:
            move = (last_close - entry_price) / entry_price * 100
            if side == "short":
                move = -move
            ret = move / 100 - fee_pct / 100
            equity *= 1 + ret
            trades += 1
            if ret > 0:
                wins += 1
            ctx.in_position = False
            side = None
            ctx.position_side = None

        peak = max(peak, equity)
        if peak > 0:
            dd = (peak - equity) / peak
            max_dd = max(max_dd, dd)

    net_return_pct = (equity - 1.0) * 100
    win_rate = wins / trades if trades else 0.0
    # Score: prefer steady winners with low drawdown.
    score = float(np.tanh(net_return_pct / 5)) * (0.5 + 0.5 * win_rate) - max_dd
    return BacktestResult(trades, wins, net_return_pct, win_rate, max_dd * 100, score)
