"""Market data service.

Public REST helpers backed by Bybit. Wrapped in a TTL cache: Redis when
configured (multi-instance, shared across processes), in-process dict
otherwise. The trading engine is the source of truth for fills — these
endpoints exist purely for UI surfaces.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.config import settings

_TICKER_TTL = 5
_CANDLE_TTL = 30


class _MemoryCache:
    """Tiny single-process TTL cache. async-compatible signature mirrors redis.asyncio."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float]] = {}

    async def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if not entry:
            return None
        value, expires_at = entry
        if expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ex: int) -> None:
        self._store[key] = (value, time.monotonic() + ex)


class _CacheProxy:
    """Lazily instantiate Redis if configured, else use in-process memory."""

    def __init__(self) -> None:
        self._impl: Any = None

    def _resolve(self) -> Any:
        if self._impl is not None:
            return self._impl
        if settings.REDIS_URL:
            import redis.asyncio as redis  # local import: only paid when used

            self._impl = redis.from_url(settings.REDIS_URL, decode_responses=True)
        else:
            self._impl = _MemoryCache()
        return self._impl

    async def get(self, key: str) -> str | None:
        return await self._resolve().get(key)

    async def set(self, key: str, value: str, ex: int) -> None:
        await self._resolve().set(key, value, ex=ex)


_cache = _CacheProxy()


async def get_public_ticker(symbol: str) -> dict[str, Any]:
    cache_key = f"ticker:{symbol.upper()}"
    cached = await _cache.get(cache_key)
    if cached:
        return json.loads(cached)
    async with httpx.AsyncClient(base_url=settings.BYBIT_REST_URL, timeout=10.0) as h:
        resp = await h.get("/v5/market/tickers", params={"category": "linear", "symbol": symbol})
        data = resp.json()
    if data.get("retCode") not in (0, "0"):
        raise RuntimeError(f"market data: {data.get('retMsg')}")
    items = data.get("result", {}).get("list") or []
    if not items:
        raise RuntimeError(f"unknown symbol {symbol}")
    t = items[0]
    out = {
        "symbol": t["symbol"],
        "price": float(t["lastPrice"]),
        "change_24h_pct": float(t["price24hPcnt"]) * 100,
        "volume_24h": float(t["turnover24h"]),
        "high_24h": float(t["highPrice24h"]),
        "low_24h": float(t["lowPrice24h"]),
    }
    await _cache.set(cache_key, json.dumps(out), ex=_TICKER_TTL)
    return out


async def get_public_candles(symbol: str, interval: str = "15m", limit: int = 200) -> list[dict[str, Any]]:
    cache_key = f"candles:{symbol.upper()}:{interval}:{limit}"
    cached = await _cache.get(cache_key)
    if cached:
        return json.loads(cached)
    bybit_interval = {
        "1m": "1",
        "3m": "3",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "4h": "240",
        "1d": "D",
    }.get(interval, interval)
    async with httpx.AsyncClient(base_url=settings.BYBIT_REST_URL, timeout=10.0) as h:
        resp = await h.get(
            "/v5/market/kline",
            params={
                "category": "linear",
                "symbol": symbol,
                "interval": bybit_interval,
                "limit": limit,
            },
        )
        data = resp.json()
    rows = list(reversed(data.get("result", {}).get("list") or []))
    out = [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
        for r in rows
    ]
    await _cache.set(cache_key, json.dumps(out), ex=_CANDLE_TTL)
    return out
