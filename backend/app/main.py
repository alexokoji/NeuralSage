"""FastAPI entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager

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

    yield

    _sched_shutdown()
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
    return JSONResponse(
        status_code=500,
        content={"detail": "internal server error" if not settings.APP_DEBUG else str(exc)},
    )


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
