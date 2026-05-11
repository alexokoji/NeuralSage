"""Celery tasks driving the trading engine."""
from __future__ import annotations

import asyncio
import uuid

from loguru import logger
from sqlalchemy import select

from app.database import SessionLocal
from app.models.agent import Agent
from app.services.trading_engine import TradingEngine
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.trading_loop.dispatch_active_agents")
def dispatch_active_agents() -> dict:
    """Find every active agent and dispatch a tick task per agent."""
    return asyncio.run(_dispatch())


async def _dispatch() -> dict:
    async with SessionLocal() as db:
        result = await db.execute(select(Agent.id).where(Agent.status == "active"))
        ids = [str(r) for r in result.scalars().all()]
    for agent_id in ids:
        run_agent_tick.delay(agent_id)
    return {"dispatched": len(ids)}


@celery_app.task(
    name="app.workers.trading_loop.run_agent_tick",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def run_agent_tick(agent_id: str) -> dict:
    return asyncio.run(_run_one(uuid.UUID(agent_id)))


async def _run_one(agent_id: uuid.UUID) -> dict:
    async with SessionLocal() as db:
        agent = await db.get(Agent, agent_id)
        if not agent or agent.status != "active":
            return {"skipped": True, "reason": "not active"}
        if not agent.api_key_id:
            return {"skipped": True, "reason": "no api key"}
        from app.models.api_key import ApiKey

        api_key = await db.get(ApiKey, agent.api_key_id)
        if not api_key:
            return {"skipped": True, "reason": "api key missing"}
        engine = TradingEngine(db)
        try:
            return await engine.run_agent_tick(agent, api_key)
        except Exception as exc:  # noqa: BLE001
            logger.exception("trading tick failed for agent {}: {}", agent_id, exc)
            raise
