"""Cross-agent learning service.

Every agent's optimization run produces a `StrategyObservation`. Future
optimization runs on the SAME strategy + symbol + timeframe (from any
user's agent) start by seeding the Bayesian optimizer with the top
historical observations — so successful parameter sets propagate
through the fleet without compromising per-agent risk caps.

Trust score (higher = more credible):

    trust = backtest_score
          + 0.5 * tanh(realized_pnl / 100)   # bounded contribution from real PnL
          + 0.2 * tanh(realized_trades / 10) # confidence boost as sample grows

This guarantees backtest-only observations still rank, but real-money
outcomes outweigh them once enough trades have run.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strategy_observation import StrategyObservation


def trust_score(obs: StrategyObservation) -> float:
    backtest = float(obs.backtest_score or 0)
    realized = float(obs.realized_pnl or 0)
    trades = int(obs.realized_trades or 0)
    return backtest + 0.5 * math.tanh(realized / 100) + 0.2 * math.tanh(trades / 10)


class LearningService:
    """All read/write paths for the shared optimization knowledge base."""

    @staticmethod
    async def record_observation(
        db: AsyncSession,
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
        db.add(obs)
        await db.commit()
        await db.refresh(obs)
        return obs

    @staticmethod
    async def update_realized(
        db: AsyncSession,
        observation_id: uuid.UUID,
        *,
        pnl_delta: float,
        trades_delta: int,
    ) -> None:
        obs = await db.get(StrategyObservation, observation_id)
        if obs is None:
            return
        obs.realized_pnl = float(obs.realized_pnl or 0) + pnl_delta
        obs.realized_trades = int(obs.realized_trades or 0) + trades_delta
        obs.updated_at = datetime.now(timezone.utc)
        await db.commit()

    @staticmethod
    async def warm_starts(
        db: AsyncSession,
        *,
        strategy_type: str,
        symbol: str,
        timeframe: str,
        n: int = 8,
        min_pool_for_strict_match: int = 3,
    ) -> list[dict[str, Any]]:
        """Top-N parameter sets to seed Bayesian optimization.

        Prefer observations matching the exact (strategy, symbol, timeframe).
        If there are fewer than `min_pool_for_strict_match`, broaden to all
        observations of that strategy type so the optimizer still gets a
        non-empty warm start in early fleet usage.
        """
        strict = await db.execute(
            select(StrategyObservation).where(
                and_(
                    StrategyObservation.strategy_type == strategy_type,
                    StrategyObservation.symbol == symbol,
                    StrategyObservation.timeframe == timeframe,
                )
            )
        )
        pool: list[StrategyObservation] = list(strict.scalars().all())

        if len(pool) < min_pool_for_strict_match:
            broad = await db.execute(
                select(StrategyObservation).where(
                    StrategyObservation.strategy_type == strategy_type
                )
            )
            pool = list(broad.scalars().all())

        if not pool:
            return []

        pool.sort(key=trust_score, reverse=True)
        return [dict(o.params) for o in pool[:n]]

    @staticmethod
    async def fleet_best(
        db: AsyncSession,
        *,
        strategy_type: str,
        symbol: str | None = None,
        timeframe: str | None = None,
        limit: int = 5,
    ) -> list[StrategyObservation]:
        """Browse the top observations — used by the UI's "what the fleet knows" view."""
        q = select(StrategyObservation).where(StrategyObservation.strategy_type == strategy_type)
        if symbol:
            q = q.where(StrategyObservation.symbol == symbol)
        if timeframe:
            q = q.where(StrategyObservation.timeframe == timeframe)
        rows = (await db.execute(q)).scalars().all()
        rows = sorted(rows, key=trust_score, reverse=True)
        return rows[:limit]


def coerce_warm_starts(
    warm_starts: Iterable[dict[str, Any]],
    *,
    keys: list[str],
    bounds: dict[str, tuple[float, float]],
) -> list[list[float]]:
    """Convert dict-shaped warm starts into the list-of-lists form gp_minimize wants.

    - Missing keys are filled with the bound midpoint (so observations from
      slightly-different parameter spaces still contribute).
    - Each value is clipped to its bound; out-of-range entries don't crash skopt.
    """
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
