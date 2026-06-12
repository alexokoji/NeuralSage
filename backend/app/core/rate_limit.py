"""Rate limiter wired to slowapi.

Storage backend:
  * `memory://` (default) when REDIS_URL is unset — single-instance only.
  * `redis://...` when REDIS_URL is set — survives restarts, multi-instance safe.
"""
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


def _resolve_storage() -> str:
    url = settings.REDIS_URL
    if not url:
        return "memory://"
    try:
        import redis as _r
        c = _r.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        c.ping()
        c.close()
        return url
    except Exception:
        return "memory://"


limiter = Limiter(
    key_func=_key_func,
    storage_uri=_resolve_storage(),
    default_limits=[settings.RATE_LIMIT_DEFAULT],
)
