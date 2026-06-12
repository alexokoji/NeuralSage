"""Periodic AI optimization sweep — MongoDB/Beanie edition."""
from __future__ import annotations

import asyncio

from loguru import logger

from app.models.agent import Agent
from app.models.api_key import ApiKey
from app.services.ai_optimizer import optimize_strategy_async
from app.services.exchange import build_client
from app.services.learning import LearningService
from app.services.strategy import get_strategy
from app.services.strategy.indicators import candles_to_df
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.optimization.optimize_active_agents")
def optimize_active_agents() -> dict:
    return asyncio.run(_run())


async def _run() -> dict:
    tuned = 0

    agents = await Agent.find(
        Agent.ai_optimization_enabled == True,  # noqa: E712
        Agent.status.in_(["active", "paused"]),
    ).to_list()

    for agent in agents:
        if not agent.strategy or not agent.api_key_id or not agent.trading_pairs:
            continue
        api_key = await ApiKey.get(agent.api_key_id)
        if not api_key:
            continue

        strat = get_strategy(agent.strategy.type)
        symbol = agent.trading_pairs[0]
        client = build_client(api_key)
        try:
            try:
                candles = await client.get_candles(symbol, agent.timeframe, limit=400)
            except Exception as exc:  # noqa: BLE001
                logger.warning("optimize: candle fetch failed for {}: {}", agent.id, exc)
                continue
        finally:
            await client.close()

        if len(candles) < 100:
            continue

        df = candles_to_df(candles)
        base = {**(strat.default_params or {}), **(agent.strategy_params or {})}
        warm_starts = await LearningService.warm_starts(
            strategy_type=agent.strategy.type,
            symbol=symbol,
            timeframe=agent.timeframe,
        )

        try:
            result = await optimize_strategy_async(strat, df, base, symbol=symbol, timeframe=agent.timeframe, warm_starts=warm_starts, n_calls=20)
        except Exception as exc:  # noqa: BLE001
            logger.exception("optimize failed for {}: {}", agent.id, exc)
            continue

        agent.strategy_params = result.best_params
        agent.optimization_params = {
            "score": result.best_score,
            "iterations": result.iterations,
            "warm_starts_used": result.warm_starts_used,
            "history_tail": result.history[-10:],
        }
        await agent.save()
        tuned += 1

    return {"agents_tuned": tuned}
