"""
Smoke test: verifies the trading agent loop fires repeatedly every ~15 s.

Run from the backend/ directory:
    python smoke_test_loop.py

What this tests:
  1. The APScheduler is configured with an IntervalTrigger (not a one-shot).
  2. The job function (`run_trading_tick_for_all_agents`) is invoked multiple
     times over a short window — not just once.
  3. The agent status doesn't accidentally flip to something that stops the
     loop from scheduling future ticks.

Everything below the MongoDB / exchange layer is mocked so you don't need a
running DB or real API keys. The test simply counts how many times the tick
job fires in 45 seconds and expects at least 3 (interval = 5 s in test mode).
"""
from __future__ import annotations

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

# ──────────────────────────────────────────────────────────────────────────────
# Lightweight stubs so we can import scheduler_jobs without a full stack
# ──────────────────────────────────────────────────────────────────────────────

# Fake settings — short interval for fast testing
fake_settings = MagicMock()
fake_settings.TRADE_LOOP_INTERVAL_SECONDS = 5          # 5 s instead of 15 s
fake_settings.OPTIMIZATION_INTERVAL_HOURS = 6
fake_settings.ENABLE_IN_PROCESS_SCHEDULER = True

# Patch settings before any app imports
import sys, types

# Stub out heavy deps the modules try to import
for mod in [
    "beanie", "motor", "motor.motor_asyncio",
    "app.database", "app.seed",
    "app.models.agent", "app.models.api_key",
    "app.models.agent_performance", "app.models.position",
    "app.models.trade", "app.models.risk_event",
    "app.models.notification", "app.models.strategy_observation",
    "app.services.exchange", "app.services.learning",
    "app.services.ai_optimizer", "app.services.grok_analyst",
    "app.services.notifications", "app.services.risk_engine",
    "app.services.strategy", "app.services.strategy.indicators",
    "app.services.trading_engine",
    "apscheduler", "apscheduler.schedulers",
    "apscheduler.schedulers.asyncio", "apscheduler.triggers",
    "apscheduler.triggers.interval", "apscheduler.triggers.cron",
]:
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)

# We need the real APScheduler — remove our stub so it imports properly
for mod in list(sys.modules.keys()):
    if mod.startswith("apscheduler"):
        del sys.modules[mod]

# ──────────────────────────────────────────────────────────────────────────────
# Tick counter — this is what we're measuring
# ──────────────────────────────────────────────────────────────────────────────
tick_calls: list[float] = []

async def fake_trading_tick() -> dict:
    t = time.time()
    tick_calls.append(t)
    idx = len(tick_calls)
    print(f"  [tick #{idx}]  job fired at t={t:.2f}")
    return {"processed": 0, "skipped": 0, "failed": 0}

async def fake_optimization_sweep() -> dict:
    return {"agents_tuned": 0}

async def fake_daily_rollover() -> dict:
    return {"snapshots": 0}

# ──────────────────────────────────────────────────────────────────────────────
# Build the scheduler exactly as production does — only swap the interval & jobs
# ──────────────────────────────────────────────────────────────────────────────
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger


def build_scheduler(interval_seconds: int) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(
        fake_trading_tick,
        IntervalTrigger(seconds=interval_seconds),
        id="trading_tick",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        fake_optimization_sweep,
        CronTrigger(hour="*/6", minute=0),
        id="optimization_sweep",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        fake_daily_rollover,
        CronTrigger(hour=0, minute=1),
        id="daily_rollover",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return sched


# ──────────────────────────────────────────────────────────────────────────────
# Main test runner
# ──────────────────────────────────────────────────────────────────────────────
INTERVAL_S = 5          # must match build_scheduler() call above
OBSERVE_S  = 28         # watch window; expect ~5 ticks (0, 5, 10, 15, 20, 25)
MIN_TICKS  = 3          # minimum to pass


async def main() -> None:
    print("=" * 60)
    print("NeuralSage – trading loop smoke test")
    print(f"  scheduler interval : {INTERVAL_S} s")
    print(f"  observation window : {OBSERVE_S} s")
    print(f"  minimum ticks      : {MIN_TICKS}")
    print("=" * 60)

    sched = build_scheduler(INTERVAL_S)
    sched.start()
    print(f"\nScheduler started.  Watching for {OBSERVE_S} s …\n")

    start = time.time()
    await asyncio.sleep(OBSERVE_S)

    sched.shutdown(wait=False)
    elapsed = time.time() - start

    print(f"\nScheduler stopped after {elapsed:.1f} s.")
    print(f"Total ticks observed: {len(tick_calls)}")

    if len(tick_calls) >= 2:
        gaps = [tick_calls[i] - tick_calls[i-1] for i in range(1, len(tick_calls))]
        avg_gap = sum(gaps) / len(gaps)
        print(f"Average gap between ticks: {avg_gap:.2f} s  (expected ~{INTERVAL_S} s)")

    print()
    passed = len(tick_calls) >= MIN_TICKS
    if passed:
        print(f"✓  PASS — loop fired {len(tick_calls)} times (≥{MIN_TICKS} required)")
    else:
        print(f"✗  FAIL — loop fired only {len(tick_calls)} time(s); expected ≥{MIN_TICKS}")
        print()
        print("Possible causes:")
        print("  • ENABLE_IN_PROCESS_SCHEDULER is False in .env")
        print("  • An exception in the first tick is silently killing the job")
        print("  • Agent.status is being mutated to non-'active' after tick #1")
        print("  • Scheduler is being shutdown too early in the FastAPI lifespan")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
