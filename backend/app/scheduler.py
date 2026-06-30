"""In-process scheduler.

Uses APScheduler's asyncio scheduler so all jobs share FastAPI's event
loop. `max_instances=1` + `coalesce=True` guarantee that a slow tick
never stacks up — late fires are dropped instead of queueing.

Started/stopped from FastAPI's lifespan (app/main.py).
"""
from __future__ import annotations

from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from app.config import settings
from app.services.scheduler_jobs import (
    check_missed_rollover,
    reconcile_exchange_positions,
    run_daily_rollover,
    run_optimization_sweep,
    run_trading_tick_for_all_agents,
    keep_alive_ping,
)

_scheduler: Optional[AsyncIOScheduler] = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def start() -> None:
    sched = get_scheduler()
    if sched.running:
        return

    sched.add_job(
        run_trading_tick_for_all_agents,
        IntervalTrigger(seconds=settings.TRADE_LOOP_INTERVAL_SECONDS),
        id="trading_tick",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        run_optimization_sweep,
        CronTrigger(hour=f"*/{settings.OPTIMIZATION_INTERVAL_HOURS}", minute=0),
        id="optimization_sweep",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        reconcile_exchange_positions,
        IntervalTrigger(minutes=2),
        id="exchange_reconciliation",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        run_daily_rollover,
        CronTrigger(hour=0, minute=1),
        id="daily_rollover",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # Self-ping to prevent Render free tier from spinning down (15min idle timeout).
    sched.add_job(
        keep_alive_ping,
        IntervalTrigger(minutes=10),
        id="keep_alive",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # Check if daily rollover was missed (e.g. Render restart after midnight)
    sched.add_job(
        check_missed_rollover,
        id="startup_rollover_check",
        max_instances=1,
        replace_existing=True,
    )

    sched.start()
    logger.info(
        "in-process scheduler started: trade_tick={}s, optimization=every {}h, rollover=daily 00:01 UTC, keep_alive=10m",
        settings.TRADE_LOOP_INTERVAL_SECONDS,
        settings.OPTIMIZATION_INTERVAL_HOURS,
    )


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("in-process scheduler stopped")
    _scheduler = None
