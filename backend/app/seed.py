"""Idempotent seed of system strategies on app start."""
from __future__ import annotations

from app.models.strategy import Strategy

SYSTEM_STRATEGIES = [
    {
        "name": "EMA Crossover",
        "type": "ema_crossover",
        "description": "9/21 EMA trend-following entries and exits.",
        "default_params": {
            "fast_ema": 9,
            "slow_ema": 21,
            "stop_loss_pct": 1.5,
            "take_profit_pct": 3.0,
            "position_size_pct": 5,
            "min_confidence": 0.55,
        },
    },
    {
        "name": "RSI Entry",
        "type": "rsi_entry",
        "description": "RSI oversold/overbought rebound entries with EMA-50 trend filter.",
        "default_params": {
            "rsi_period": 14,
            "oversold": 30,
            "overbought": 70,
            "trend_ema": 50,
            "stop_loss_pct": 1.0,
            "take_profit_pct": 2.0,
            "position_size_pct": 3,
            "min_confidence": 0.5,
        },
    },
    {
        "name": "Breakout",
        "type": "breakout",
        "description": "Donchian-style range breakout, volume-confirmed.",
        "default_params": {
            "lookback_period": 20,
            "breakout_threshold_pct": 0.5,
            "volume_multiplier": 1.5,
            "stop_loss_pct": 1.2,
            "take_profit_pct": 2.5,
            "position_size_pct": 4,
            "min_confidence": 0.55,
        },
    },
    {
        "name": "Micro Scalping",
        "type": "micro_scalping",
        "description": "Very short-term mean reversion off a fast EMA, for 1m–5m TFs.",
        "default_params": {
            "ema_period": 8,
            "deviation_pct": 0.25,
            "profit_target_pct": 0.3,
            "stop_loss_pct": 0.2,
            "max_trades_per_hour": 10,
            "position_size_pct": 2,
            "min_confidence": 0.5,
        },
    },
    {
        "name": "Composite (Custom)",
        "type": "composite",
        "description": "Custom strategy built from AI-generated indicator rules. Use the Strategy Builder to create one.",
        "default_params": {
            "rules": {},
            "stop_loss_pct": 1.0,
            "take_profit_pct": 2.5,
            "min_confidence": 0.6,
            "position_size_pct": 2.0,
        },
    },
]


async def seed_strategies() -> None:
    for spec in SYSTEM_STRATEGIES:
        exists = await Strategy.find_one(Strategy.type == spec["type"])
        if exists:
            continue
        await Strategy(**spec, is_system=True).insert()
