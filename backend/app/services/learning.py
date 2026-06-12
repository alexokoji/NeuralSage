"""Cross-agent learning service — MongoDB/Beanie edition."""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from app.models.strategy_observation import StrategyObservation


def trust_score(obs: StrategyObservation) -> float:
    backtest = float(obs.backtest_score or 0)
    realized = float(obs.realized_pnl or 0)
    trades = int(obs.realized_trades or 0)
    return backtest + 0.5 * math.tanh(realized / 100) + 0.2 * math.tanh(trades / 10)


class LearningService:

    @staticmethod
    async def record_observation(
        *,
        strategy_type: str,
        symbol: str,
        timeframe: str,
        params: dict[str, Any],
        backtest_score: float,
        source_agent_id: uuid.UUID | None,
        source_user_id: uuid.UUID | None,
        candle_window_start: datetime | None = None,
        candle_window_end: datetime | None = None,
    ) -> StrategyObservation:
        obs = StrategyObservation(
            strategy_type=strategy_type,
            symbol=symbol,
            timeframe=timeframe,
            params=params,
            backtest_score=backtest_score,
            source_agent_id=source_agent_id,
            source_user_id=source_user_id,
            candle_window_start=candle_window_start,
            candle_window_end=candle_window_end,
        )
        await obs.insert()
        return obs

    @staticmethod
    async def update_realized(
        observation_id: uuid.UUID,
        *,
        pnl_delta: float,
        trades_delta: int,
    ) -> None:
        obs = await StrategyObservation.find_one(StrategyObservation.id == observation_id)
        if obs is None:
            return
        obs.realized_pnl = float(obs.realized_pnl or 0) + pnl_delta
        obs.realized_trades = int(obs.realized_trades or 0) + trades_delta
        obs.updated_at = datetime.now(timezone.utc)
        await obs.save()

    @staticmethod
    async def warm_starts(
        *,
        strategy_type: str,
        symbol: str,
        timeframe: str,
        n: int = 8,
        min_pool_for_strict_match: int = 3,
    ) -> list[dict[str, Any]]:
        pool = await StrategyObservation.find(
            StrategyObservation.strategy_type == strategy_type,
            StrategyObservation.symbol == symbol,
            StrategyObservation.timeframe == timeframe,
        ).to_list()

        if len(pool) < min_pool_for_strict_match:
            pool = await StrategyObservation.find(
                StrategyObservation.strategy_type == strategy_type
            ).to_list()

        if not pool:
            return []

        pool.sort(key=trust_score, reverse=True)
        return [dict(o.params) for o in pool[:n]]

    @staticmethod
    async def fleet_best(
        *,
        strategy_type: str,
        symbol: str | None = None,
        timeframe: str | None = None,
        limit: int = 5,
    ) -> list[StrategyObservation]:
        query = StrategyObservation.find(StrategyObservation.strategy_type == strategy_type)
        if symbol:
            query = query.find(StrategyObservation.symbol == symbol)
        if timeframe:
            query = query.find(StrategyObservation.timeframe == timeframe)
        rows = await query.to_list()
        rows.sort(key=trust_score, reverse=True)
        return rows[:limit]


def coerce_warm_starts(
    warm_starts: Iterable[dict[str, Any]],
    *,
    keys: list[str],
    bounds: dict[str, tuple[float, float]],
) -> list[list[float]]:
    points: list[list[float]] = []
    for ws in warm_starts:
        point = []
        ok = True
        for k in keys:
            lo, hi = bounds[k]
            if k in ws:
                try:
                    v = float(ws[k])
                except (TypeError, ValueError):
                    ok = False
                    break
            else:
                v = (lo + hi) / 2
            point.append(max(lo, min(hi, v)))
        if ok and len(point) == len(keys):
            points.append(point)
    return points
