"""Notification persistence and (optional) Redis pub/sub fanout."""
from __future__ import annotations

import json
from typing import Any

import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.notification import Notification

_redis_client: redis.Redis | None = None


def _redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


class NotificationService:
    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        user_id,
        type: str,
        title: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> Notification:
        n = Notification(
            user_id=user_id,
            type=type,
            title=title,
            message=message,
            data=data or {},
        )
        db.add(n)
        await db.commit()
        await db.refresh(n)

        # Best-effort fanout — failures are not propagated.
        try:
            await _redis().publish(
                f"notif:user:{user_id}",
                json.dumps(
                    {
                        "id": str(n.id),
                        "type": n.type,
                        "title": n.title,
                        "message": n.message,
                        "data": n.data,
                        "created_at": n.created_at.isoformat() if n.created_at else None,
                    }
                ),
            )
        except Exception:  # noqa: BLE001
            pass
        return n
