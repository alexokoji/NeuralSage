"""Notification persistence and (optional) Redis pub/sub fanout.

Falls back silently when REDIS_URL is unset — the DB row is still
persisted, just no real-time broadcast. UIs polling `GET /notifications`
still see new entries promptly.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.notification import Notification

_redis_client: Any | None = None


def _redis() -> Any | None:
    global _redis_client
    if not settings.REDIS_URL:
        return None
    if _redis_client is None:
        import redis.asyncio as redis  # local import — only paid when Redis is configured

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

        # Best-effort fanout — failures (or no Redis at all) are silent.
        client = _redis()
        if client is not None:
            try:
                await client.publish(
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
