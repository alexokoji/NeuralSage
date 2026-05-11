"""Celery app + beat schedule.

Tasks:
  * agent_trade_tick : run trading engine for each active agent on a fixed cadence
  * optimize_agents  : periodically retune AI-enabled agent params
  * roll_daily_pnl   : reset agent.current_day_pnl at 00:00 UTC + snapshot performance
"""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "neuraltrade",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.workers.trading_loop",
        "app.workers.optimization",
        "app.workers.daily_rollover",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_default_retry_delay=10,
    worker_prefetch_multiplier=1,
)

celery_app.conf.beat_schedule = {
    "agent-trade-tick": {
        "task": "app.workers.trading_loop.dispatch_active_agents",
        "schedule": settings.TRADE_LOOP_INTERVAL_SECONDS,
    },
    "agent-optimization": {
        "task": "app.workers.optimization.optimize_active_agents",
        "schedule": crontab(minute=0, hour=f"*/{settings.OPTIMIZATION_INTERVAL_HOURS}"),
    },
    "daily-rollover": {
        "task": "app.workers.daily_rollover.rollover_daily_pnl",
        "schedule": crontab(minute=1, hour=0),
    },
}
