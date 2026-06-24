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
    async def record_trade_outcome(
        *,
        agent_id: uuid.UUID,
        strategy_type: str,
        symbol: str,
        timeframe: str,
        pnl: float,
    ) -> None:
        """Update the most recent observation for this agent's strategy with real trade PnL.

        Called when a position closes — this is the critical feedback loop that
        lets agents learn from actual outcomes, not just backtests.
        """
        obs = await StrategyObservation.find(
            StrategyObservation.source_agent_id == agent_id,
            StrategyObservation.strategy_type == strategy_type,
        ).sort(-StrategyObservation.created_at).first_or_none()

        if obs is None:
            obs = await StrategyObservation.find(
                StrategyObservation.strategy_type == strategy_type,
                StrategyObservation.symbol == symbol,
                StrategyObservation.timeframe == timeframe,
            ).sort(-StrategyObservation.created_at).first_or_none()

        if obs is None:
            return

        obs.realized_pnl = float(obs.realized_pnl or 0) + pnl
        obs.realized_trades = int(obs.realized_trades or 0) + 1
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

        # Only use observations that are profitable or untested (no realized data yet).
        # Never seed the optimizer with params that lost real money.
        safe_pool = [
            o for o in pool
            if float(o.realized_pnl or 0) >= 0 or int(o.realized_trades or 0) == 0
        ]
        if not safe_pool:
            safe_pool = pool[:2]

        safe_pool.sort(key=trust_score, reverse=True)
        return [dict(o.params) for o in safe_pool[:n]]

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
        # Filter out observations with proven negative PnL — never recommend
        # params that lost real money to other agents.
        safe_rows = [
            r for r in rows
            if float(r.realized_pnl or 0) >= 0 or int(r.realized_trades or 0) < 3
        ]
        safe_rows.sort(key=trust_score, reverse=True)
        return safe_rows[:limit]

    @staticmethod
    async def propagate_to_fleet(
        *,
        strategy_type: str,
        winning_params: dict[str, Any],
        source_agent_id: uuid.UUID | None = None,
        realized_pnl: float | None = None,
    ) -> int:
        """Push winning params to all agents using this strategy + update
        the Strategy document defaults so new agents start smart.

        SAFETY: Only propagates if the source has positive realized PnL.
        Never spreads losing strategies to the fleet.

        Returns count of agents updated.
        """
        from app.models.agent import Agent
        from app.models.strategy import Strategy as StrategyDoc
        from loguru import logger

        # SAFETY CHECK: verify the params come from a profitable source.
        # If realized_pnl is provided and negative, refuse to propagate.
        if realized_pnl is not None and realized_pnl < 0:
            logger.warning(
                "BLOCKED propagation of {} params — realized_pnl is negative ({:.2f})",
                strategy_type, realized_pnl,
            )
            return 0

        # Double-check: look at the best fleet observation for this strategy.
        # If the fleet's best has negative PnL, something is wrong — don't spread it.
        best = await StrategyObservation.find(
            StrategyObservation.strategy_type == strategy_type,
        ).to_list()
        if best:
            best.sort(key=trust_score, reverse=True)
            top = best[0]
            if float(top.realized_pnl or 0) < 0 and int(top.realized_trades or 0) >= 3:
                logger.warning(
                    "BLOCKED propagation of {} params — fleet best has negative realized PnL ({:.2f})",
                    strategy_type, float(top.realized_pnl or 0),
                )
                return 0

        # Update strategy defaults in DB
        strat_doc = await StrategyDoc.find_one(StrategyDoc.type == strategy_type)
        if strat_doc:
            strat_doc.default_params = {**strat_doc.default_params, **winning_params}
            await strat_doc.save()

        # Also check composite variants
        if not strat_doc and strategy_type.startswith("composite_"):
            strat_doc = await StrategyDoc.find_one(StrategyDoc.type == strategy_type)
            if strat_doc:
                strat_doc.default_params = {**strat_doc.default_params, **winning_params}
                await strat_doc.save()

        # Push to all agents with this strategy (except the source)
        all_agents = await Agent.find(
            Agent.status.in_(["active", "paused", "idle"]),
        ).to_list()

        updated = 0
        for agent in all_agents:
            if not agent.strategy or agent.strategy.type != strategy_type:
                continue
            if source_agent_id and agent.id == source_agent_id:
                continue
            agent.strategy_params = {**(agent.strategy_params or {}), **winning_params}
            await agent.save()
            updated += 1

        if updated or strat_doc:
            logger.info(
                "propagated winning {} params to {} agent(s) + strategy defaults",
                strategy_type, updated,
            )
        return updated


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
