"""Market data service.

Public REST helpers. Cascade: Bybit mainnet → OKX spot (when Bybit's
Cloudflare WAF blocks cloud-hosted server IPs). Wrapped in a TTL cache
(Redis when configured, in-process dict otherwise). The trading engine
is the source of truth for fills — these endpoints exist for UI surfaces.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.config import settings

_TICKER_TTL = 5
_CANDLE_TTL = 30
_OKX_URL = "https://www.okx.com"

_BYBIT_INTERVAL_MAP = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15",
    "30m": "30", "1h": "60", "4h": "240", "1d": "D",
}
_OKX_INTERVAL_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D",
}


def _to_okx(symbol: str) -> str:
    s = symbol.upper()
    return (s[:-4] + "-USDT") if s.endswith("USDT") else s


class _MemoryCache:
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
    def __init__(self) -> None:
        self._impl: Any = None

    def _resolve(self) -> Any:
        if self._impl is not None:
            return self._impl
        if settings.REDIS_URL:
            import redis.asyncio as redis
            self._impl = redis.from_url(settings.REDIS_URL, decode_responses=True)
        else:
            self._impl = _MemoryCache()
        return self._impl

    async def get(self, key: str) -> str | None:
        return await self._resolve().get(key)

    async def set(self, key: str, value: str, ex: int) -> None:
        await self._resolve().set(key, value, ex=ex)


_cache = _CacheProxy()


async def _bybit_ticker(symbol: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=settings.BYBIT_REST_URL, timeout=10.0) as h:
        resp = await h.get("/v5/market/tickers", params={"category": "linear", "symbol": symbol})
        data = resp.json()
    if data.get("retCode") not in (0, "0"):
        raise RuntimeError(f"bybit ticker: {data.get('retMsg')}")
    items = (data.get("result") or {}).get("list") or []
    if not items:
        raise RuntimeError(f"bybit: unknown symbol {symbol}")
    t = items[0]
    return {
        "symbol": t["symbol"],
        "price": float(t["lastPrice"]),
        "change_24h_pct": float(t["price24hPcnt"]) * 100,
        "volume_24h": float(t["turnover24h"]),
        "high_24h": float(t["highPrice24h"]),
        "low_24h": float(t["lowPrice24h"]),
    }


async def _okx_ticker(symbol: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=_OKX_URL, timeout=10.0) as h:
        resp = await h.get("/api/v5/market/ticker", params={"instId": _to_okx(symbol)})
        data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"okx ticker: {data.get('msg')}")
    items = data.get("data") or []
    if not items:
        raise RuntimeError(f"okx: no ticker for {symbol}")
    t = items[0]
    last = float(t["last"])
    open24 = float(t.get("open24h") or last)
    change_pct = ((last - open24) / open24 * 100) if open24 else 0.0
    return {
        "symbol": symbol.upper(),
        "price": last,
        "change_24h_pct": change_pct,
        "volume_24h": float(t.get("volCcy24h") or 0),
        "high_24h": float(t.get("high24h") or last),
        "low_24h": float(t.get("low24h") or last),
    }


async def _bybit_candles(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
    bybit_interval = _BYBIT_INTERVAL_MAP.get(interval, interval)
    async with httpx.AsyncClient(base_url=settings.BYBIT_REST_URL, timeout=10.0) as h:
        resp = await h.get(
            "/v5/market/kline",
            params={"category": "linear", "symbol": symbol, "interval": bybit_interval, "limit": limit},
        )
        data = resp.json()
    rows = list(reversed((data.get("result") or {}).get("list") or []))
    return [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
        for r in rows
    ]


async def _okx_candles(symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
    bar = _OKX_INTERVAL_MAP.get(interval, "15m")
    async with httpx.AsyncClient(base_url=_OKX_URL, timeout=10.0) as h:
        resp = await h.get(
            "/api/v5/market/candles",
            params={"instId": _to_okx(symbol), "bar": bar, "limit": min(limit, 300)},
        )
        data = resp.json()
    if data.get("code") != "0":
        raise RuntimeError(f"okx candles: {data.get('msg')}")
    rows = list(reversed(data.get("data") or []))
    return [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
        for r in rows
    ]


async def get_public_ticker(symbol: str) -> dict[str, Any]:
    cache_key = f"ticker:{symbol.upper()}"
    cached = await _cache.get(cache_key)
    if cached:
        return json.loads(cached)
    try:
        out = await _bybit_ticker(symbol)
    except Exception:
        out = await _okx_ticker(symbol)
    await _cache.set(cache_key, json.dumps(out), ex=_TICKER_TTL)
    return out


async def get_public_candles(symbol: str, interval: str = "15m", limit: int = 200) -> list[dict[str, Any]]:
    cache_key = f"candles:{symbol.upper()}:{interval}:{limit}"
    cached = await _cache.get(cache_key)
    if cached:
        return json.loads(cached)
    try:
        out = await _bybit_candles(symbol, interval, limit)
        if not out:
            raise RuntimeError("empty response")
    except Exception:
        out = await _okx_candles(symbol, interval, limit)
    await _cache.set(cache_key, json.dumps(out), ex=_CANDLE_TTL)
    return out
