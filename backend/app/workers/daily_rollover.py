"""Daily rollover: snapshot performance, reset day-PnL counters — MongoDB/Beanie edition."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from app.models.agent import Agent
from app.models.agent_performance import AgentPerformance
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.daily_rollover.rollover_daily_pnl")
def rollover_daily_pnl() -> dict:
    return asyncio.run(_run())


async def _run() -> dict:
    snapshot_count = 0
    today = date.today()

    agents = await Agent.find_all().to_list()
    for agent in agents:
        cap = float(agent.assigned_capital or 0)
        day_pnl = float(agent.current_day_pnl or 0)
        wins = int(agent.winning_trades or 0)
        total = int(agent.total_trades or 0)
        losses = max(0, total - wins)

        await AgentPerformance(
            agent_id=agent.id,
            user_id=agent.user_id,
            snapshot_date=today,
            starting_capital=cap,
            ending_capital=cap + day_pnl,
            daily_pnl=day_pnl,
            daily_pnl_pct=(day_pnl / cap * 100) if cap > 0 else 0,
            total_trades=total,
            winning_trades=wins,
            losing_trades=losses,
            win_rate=(wins / total) if total else 0,
            strategy_params_snapshot=agent.strategy_params or {},
        ).insert()

        agent.current_day_pnl = 0
        await agent.save()
        snapshot_count += 1

    return {"snapshots": snapshot_count, "at": datetime.now(timezone.utc).isoformat()}
