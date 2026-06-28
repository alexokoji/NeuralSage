"""MongoDB connection and Beanie initialisation."""
from __future__ import annotations

from typing import Any

from beanie import init_beanie
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import ConfigurationError, PyMongoError

from app.config import settings

_motor_client: AsyncIOMotorClient | None = None


def get_motor_client() -> AsyncIOMotorClient:
    global _motor_client
    if _motor_client is None:
        _motor_client = AsyncIOMotorClient(settings.MONGODB_URL)
    return _motor_client


async def init_db() -> None:
    """Initialise Beanie with all document models. Called once at app startup."""
    from app.models.user import User
    from app.models.api_key import ApiKey
    from app.models.strategy import Strategy
    from app.models.agent import Agent
    from app.models.trade import Trade
    from app.models.position import Position
    from app.models.agent_performance import AgentPerformance
    from app.models.risk_event import RiskEvent
    from app.models.notification import Notification
    from app.models.strategy_observation import StrategyObservation

    try:
        client = get_motor_client()
        db = client[settings.MONGODB_DB_NAME]
        await init_beanie(
            database=db,
            document_models=[
                User,
                ApiKey,
                Strategy,
                Agent,
                Trade,
                Position,
                AgentPerformance,
                RiskEvent,
                Notification,
                StrategyObservation,
            ],
        )
    except (ConfigurationError, PyMongoError, OSError, ValueError) as exc:
        logger.warning("database bootstrap skipped: {}", exc)


async def close_db() -> None:
    global _motor_client
    if _motor_client is not None:
        _motor_client.close()
        _motor_client = None
