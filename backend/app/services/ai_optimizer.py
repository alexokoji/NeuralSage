"""AI Optimization Engine.

The optimizer's job is STRICTLY to tune parameters of an existing
strategy: stop loss, take profit, entry thresholds, position size %.
It does NOT decide whether to trade — that remains the strategy's
job, gated by the risk engine.

Implementation: scikit-optimize gp_minimize (Bayesian optimization with
a Gaussian process surrogate) over a bounded search space.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
from skopt import gp_minimize
from skopt.space import Real

from app.services.backtester import backtest
from app.services.strategy.base import Strategy

# Per-strategy tunable bounds. Anything outside these will not be searched.
SEARCH_SPACES: dict[str, dict[str, tuple[float, float]]] = {
    "ema_crossover": {
        "stop_loss_pct": (0.5, 3.0),
        "take_profit_pct": (1.0, 6.0),
        "min_confidence": (0.4, 0.8),
    },
    "rsi_entry": {
        "stop_loss_pct": (0.3, 2.5),
        "take_profit_pct": (0.6, 4.0),
        "oversold": (20.0, 35.0),
        "overbought": (65.0, 80.0),
        "min_confidence": (0.4, 0.8),
    },
    "breakout": {
        "stop_loss_pct": (0.5, 2.5),
        "take_profit_pct": (1.0, 5.0),
        "breakout_threshold_pct": (0.2, 1.5),
        "volume_multiplier": (1.0, 3.0),
        "min_confidence": (0.4, 0.8),
    },
    "micro_scalping": {
        "stop_loss_pct": (0.05, 0.5),
        "profit_target_pct": (0.1, 0.8),
        "deviation_pct": (0.1, 0.6),
        "min_confidence": (0.4, 0.8),
    },
}


@dataclass
class OptimizationResult:
    best_params: dict[str, Any]
    best_score: float
    iterations: int
    history: list[dict[str, Any]]


def optimize_strategy(
    strategy: Strategy,
    candles: pd.DataFrame,
    base_params: dict[str, Any],
    *,
    n_calls: int = 25,
    random_state: int = 42,
) -> OptimizationResult:
    space_def = SEARCH_SPACES.get(strategy.type)
    if not space_def:
        return OptimizationResult(
            best_params=base_params, best_score=0.0, iterations=0, history=[]
        )

    keys = list(space_def.keys())
    space = [Real(low, high, name=k) for k, (low, high) in space_def.items()]
    history: list[dict[str, Any]] = []

    def objective(values: list[float]) -> float:
        candidate = dict(base_params)
        for k, v in zip(keys, values):
            candidate[k] = float(v)
        result = backtest(strategy, candles, candidate)
        history.append({"params": {k: float(v) for k, v in zip(keys, values)}, "score": result.score})
        return -result.score  # gp_minimize minimizes

    result = gp_minimize(
        objective,
        space,
        n_calls=max(n_calls, 10),
        random_state=random_state,
        n_initial_points=min(5, n_calls),
    )

    best = dict(base_params)
    for k, v in zip(keys, result.x):
        best[k] = float(v)
    return OptimizationResult(
        best_params=best,
        best_score=-float(result.fun),
        iterations=len(history),
        history=history[-50:],  # keep tail for inspection
    )
