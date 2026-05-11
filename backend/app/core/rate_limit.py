"""Rate limiter wired to slowapi (Redis-backed when REDIS_URL is set)."""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings


def _key_func(request) -> str:
    # Prefer authenticated user ID, fall back to IP.
    user = getattr(request.state, "user", None)
    if user is not None:
        return f"user:{user.id}"
    return get_remote_address(request)


limiter = Limiter(
    key_func=_key_func,
    storage_uri=settings.REDIS_URL,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
)
