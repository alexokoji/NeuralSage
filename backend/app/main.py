"""FastAPI entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager

import asyncio

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.v1 import api_router
from app.config import settings
from app.core.rate_limit import limiter
from app.database import close_db, init_db
from app.seed import seed_strategies


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialise MongoDB / Beanie.
    await init_db()

    # Seed system strategies on first boot.
    try:
        await seed_strategies()
    except Exception as exc:  # noqa: BLE001
        logger.warning("strategy seed skipped: {}", exc)

    # Drive the trading loop, optimization sweep, and daily rollover in-process.
    if settings.ENABLE_IN_PROCESS_SCHEDULER:
        try:
            from app.scheduler import shutdown as _sched_shutdown
            from app.scheduler import start as _sched_start

            _sched_start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed to start in-process scheduler: {}", exc)
            _sched_shutdown = lambda: None  # noqa: E731
    else:
        _sched_shutdown = lambda: None  # noqa: E731

    # Ping immediately so Render doesn't sleep before the first scheduled keep-alive.
    async def _startup_ping() -> None:
        await asyncio.sleep(5)
        try:
            from app.services.scheduler_jobs import keep_alive_ping
            await keep_alive_ping()
            logger.info("startup keep-alive ping sent")
        except Exception as exc:
            logger.debug("startup ping failed (non-fatal): {}", exc)

    asyncio.create_task(_startup_ping())

    # Seed the learning system immediately — without this, trades in the first
    # hour after deploy have their realized PnL discarded because there are no
    # StrategyObservation records to update yet.
    async def _startup_optimize() -> None:
        await asyncio.sleep(90)  # wait for DB + exchange clients to settle
        try:
            from app.services.scheduler_jobs import run_optimization_sweep
            result = await run_optimization_sweep()
            logger.info("startup optimization seeded learning DB: {}", result)
        except Exception as exc:
            logger.warning("startup optimization failed (non-fatal): {}", exc)

    asyncio.create_task(_startup_optimize())

    # Start real-time WebSocket streams for live exchange agents.
    try:
        from app.services.position_stream import start_all_active as _stream_start
        from app.services.position_stream import stop_all as _stream_stop
        await _stream_start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("position stream startup failed (non-fatal): {}", exc)
        _stream_stop = lambda: None  # noqa: E731

    yield

    _sched_shutdown()
    try:
        await _stream_stop()
    except Exception:
        pass
    await close_db()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    debug=settings.APP_DEBUG,
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error on {} {}", request.method, request.url.path)
    # Ensure CORS headers are present even on internal errors so browsers don't
    # discard the response due to missing Access-Control-Allow-Origin.
    origin = settings.cors_origins[0] if settings.cors_origins else "*"
    content = {"detail": "internal server error" if not settings.APP_DEBUG else str(exc)}
    resp = JSONResponse(status_code=500, content=content)
    try:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
    except Exception:
        pass
    return resp


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "name": settings.APP_NAME, "env": settings.APP_ENV}


@app.get("/health/scheduler")
async def health_scheduler() -> dict:
    """Diagnostic: report whether the in-process scheduler is alive and what jobs it holds."""
    if not settings.ENABLE_IN_PROCESS_SCHEDULER:
        return {"enabled": False, "running": False, "jobs": []}
    try:
        from app.scheduler import get_scheduler
        sched = get_scheduler()
        jobs = [
            {
                "id": j.id,
                "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
                "trigger": str(j.trigger),
            }
            for j in sched.get_jobs()
        ]
        return {"enabled": True, "running": sched.running, "jobs": jobs}
    except Exception as exc:  # noqa: BLE001
        return {"enabled": True, "running": False, "error": str(exc)}


app.include_router(api_router)
