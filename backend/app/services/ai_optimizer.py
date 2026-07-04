"""AI Optimization Engine.

The optimizer's job is STRICTLY to tune parameters of an existing
strategy: stop loss, take profit, entry thresholds, position size %.
It does NOT decide whether to trade — that remains the strategy's
job, gated by the risk engine.

Implementation: scikit-optimize gp_minimize (Bayesian optimization with
a Gaussian process surrogate) over a bounded search space. Optionally
warm-started from `StrategyObservation` rows produced by other agents
using the same strategy / symbol / timeframe — see learning.py.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pandas as pd
from skopt import gp_minimize
from skopt.space import Real

from app.services.backtester import backtest
import app.services.grok_analyst as grok_analyst
from app.services.learning import coerce_warm_starts
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
        "deviation_pct": (0.04, 0.20),
        "min_confidence": (0.4, 0.8),
    },
}


@dataclass
class OptimizationResult:
    best_params: dict[str, Any]
    best_score: float
    iterations: int
    history: list[dict[str, Any]]
    warm_starts_used: int = 0


async def optimize_strategy_async(
    strategy: Strategy,
    candles: pd.DataFrame,
    base_params: dict[str, Any],
    *,
    symbol: str = "UNKNOWN",
    timeframe: str = "?",
    warm_starts: list[dict[str, Any]] | None = None,
    n_calls: int = 25,
    random_state: int = 42,
    loss_context: list[dict[str, Any]] | None = None,
) -> OptimizationResult:
    """Async wrapper: fetches Grok param suggestion then calls the sync optimizer."""
    space_def = SEARCH_SPACES.get(strategy.type)
    enhanced_warm_starts = list(warm_starts or [])

    if space_def:
        grok_params = await grok_analyst.suggest_params(
            strategy.type,
            candles,
            symbol=symbol,
            timeframe=timeframe,
            search_space=space_def,
            existing_warm_starts=enhanced_warm_starts,
            loss_context=loss_context,
        )
        if grok_params:
            enhanced_warm_starts.insert(0, grok_params)  # Grok's suggestion goes first

    return optimize_strategy(
        strategy,
        candles,
        base_params,
        warm_starts=enhanced_warm_starts or None,
        n_calls=n_calls,
        random_state=random_state,
    )


def optimize_strategy(
    strategy: Strategy,
    candles: pd.DataFrame,
    base_params: dict[str, Any],
    *,
    warm_starts: list[dict[str, Any]] | None = None,
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

    # Translate cross-agent observations into seed points the optimizer can use.
    x0: list[list[float]] | None = None
    if warm_starts:
        x0 = coerce_warm_starts(warm_starts, keys=keys, bounds=space_def)
        if not x0:
            x0 = None

    def objective(values: list[float]) -> float:
        candidate = dict(base_params)
        for k, v in zip(keys, values):
            candidate[k] = float(v)
        result = backtest(strategy, candles, candidate)
        history.append({"params": {k: float(v) for k, v in zip(keys, values)}, "score": result.score})
        return -result.score  # gp_minimize minimizes

    # If warm starts are provided, gp_minimize uses them as the initial design
    # set and `n_initial_points` should be 0 to avoid double-sampling.
    if x0:
        n_calls_total = max(n_calls, len(x0) + 5)
        gp_kwargs = {"x0": x0, "n_initial_points": 0}
    else:
        n_calls_total = max(n_calls, 10)
        gp_kwargs = {"n_initial_points": min(5, n_calls)}

    result = gp_minimize(
        objective,
        space,
        n_calls=n_calls_total,
        random_state=random_state,
        **gp_kwargs,
    )

    best = dict(base_params)
    for k, v in zip(keys, result.x):
        best[k] = float(v)
    return OptimizationResult(
        best_params=best,
        best_score=-float(result.fun),
        iterations=len(history),
        history=history[-50:],
        warm_starts_used=len(x0 or []),
    )
