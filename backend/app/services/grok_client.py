"""Groq Cloud client with multi-key load balancing.

Supports multiple API keys (GROQ_API_KEY + GROQ_API_KEY_2, or comma-separated
in GROQ_API_KEY). Each key has independent rate-limit tracking so when one key
hits 429 the next key takes over — effectively doubling throughput on free tier.

Free tier per key: 30 requests/min, 14,400 requests/day.
With 2 keys: 60 requests/min, 28,800/day.

Usage
-----
    from app.services.grok_client import GrokClient
    client = GrokClient()
    text = await client.chat([{"role": "user", "content": "Hello!"}])
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

from app.config import settings

# Groq Cloud — OpenAI-compatible endpoint
_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "llama-3.3-70b-versatile"
_MINI_MODEL = "llama-3.1-8b-instant"
_TIMEOUT = 30.0
_RATE_LIMIT_BACKOFF_SECONDS = 15.0
_MIN_REQUEST_INTERVAL = 1.0


class GrokError(Exception):
    """General xAI / network error."""


class GrokUnavailableError(GrokError):
    """Raised when no API key is configured — callers should degrade gracefully."""


# ------------------------------------------------------------------ #
# Key pool — tracks per-key rate state
# ------------------------------------------------------------------ #

@dataclass
class _KeySlot:
    key: str
    label: str
    rate_limited_until: float = 0.0
    last_request_at: float = 0.0
    request_count: int = 0
    error_count: int = 0


class _KeyPool:
    """Round-robin pool of Groq API keys with per-key rate tracking."""

    def __init__(self) -> None:
        self._slots: list[_KeySlot] = []
        self._index = 0
        self._init_keys()

    def _init_keys(self) -> None:
        keys: list[str] = []
        primary = getattr(settings, "GROQ_API_KEY", "") or ""
        for part in primary.split(","):
            k = part.strip()
            if k:
                keys.append(k)
        secondary = getattr(settings, "GROQ_API_KEY_2", "") or ""
        if secondary.strip():
            keys.append(secondary.strip())

        for i, k in enumerate(keys):
            self._slots.append(_KeySlot(key=k, label=f"key_{i+1}"))

        if self._slots:
            logger.info("Groq key pool initialised with {} key(s)", len(self._slots))

    @property
    def available(self) -> bool:
        return len(self._slots) > 0

    def pick(self) -> _KeySlot | None:
        """Pick the best available key slot. Returns None if all are rate-limited."""
        if not self._slots:
            return None

        now = time.monotonic()
        n = len(self._slots)

        # Try round-robin starting from current index
        for offset in range(n):
            idx = (self._index + offset) % n
            slot = self._slots[idx]
            if slot.rate_limited_until <= now:
                self._index = (idx + 1) % n
                return slot

        # All keys rate-limited — return the one that recovers soonest
        best = min(self._slots, key=lambda s: s.rate_limited_until)
        remaining = best.rate_limited_until - now
        if remaining > 0:
            return None
        return best

    def mark_rate_limited(self, slot: _KeySlot) -> None:
        slot.rate_limited_until = time.monotonic() + _RATE_LIMIT_BACKOFF_SECONDS
        slot.error_count += 1
        logger.warning(
            "Groq {} rate-limited (429) — pausing for {}s ({} other key(s) available)",
            slot.label,
            int(_RATE_LIMIT_BACKOFF_SECONDS),
            sum(1 for s in self._slots if s is not slot and s.rate_limited_until <= time.monotonic()),
        )

    def status(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [
            {
                "label": s.label,
                "available": s.rate_limited_until <= now,
                "cooldown_remaining": max(0, s.rate_limited_until - now),
                "requests": s.request_count,
                "errors": s.error_count,
            }
            for s in self._slots
        ]


_pool = _KeyPool()


# ------------------------------------------------------------------ #
# Client
# ------------------------------------------------------------------ #

class GrokClient:
    """Thin async wrapper around Groq's chat-completions endpoint."""

    def __init__(self, model: str = _DEFAULT_MODEL) -> None:
        self._model = model
        self._mini = _MINI_MODEL
        if not _pool.available:
            raise GrokUnavailableError("No GROQ_API_KEY configured")

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        mini: bool = False,
    ) -> str:
        payload = self._build_payload(
            messages, system=system, temperature=temperature,
            max_tokens=max_tokens, mini=mini,
        )
        data = await self._post("/chat/completions", payload)
        return data["choices"][0]["message"]["content"]

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        mini: bool = False,
    ) -> Any:
        payload = self._build_payload(
            messages, system=system, temperature=temperature,
            max_tokens=max_tokens, mini=mini, json_mode=True,
        )
        data = await self._post("/chat/completions", payload)
        raw = data["choices"][0]["message"]["content"]
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GrokError(f"Grok returned non-JSON: {raw[:200]}") from exc

    def _build_payload(
        self,
        messages: list[dict[str, str]],
        *,
        system: str | None,
        temperature: float,
        max_tokens: int,
        mini: bool,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        full_messages: list[dict[str, str]] = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)
        payload: dict[str, Any] = {
            "model": self._mini if mini else self._model,
            "messages": full_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        slot = _pool.pick()
        if slot is None:
            remaining = min(s.rate_limited_until for s in _pool._slots) - time.monotonic()
            raise GrokError(
                f"All Groq keys rate-limited — shortest wait: {max(0, remaining):.0f}s"
            )

        # Proactive per-key throttle
        wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - slot.last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)

        slot.last_request_at = time.monotonic()
        slot.request_count += 1

        headers = {
            "Authorization": f"Bearer {slot.key}",
            "Content-Type": "application/json",
        }

        url = _BASE_URL + path
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise GrokError(f"xAI request timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise GrokError(f"xAI network error: {exc}") from exc

        if resp.status_code == 401:
            raise GrokUnavailableError(f"Groq {slot.label} key is invalid (401)")
        if resp.status_code == 429:
            _pool.mark_rate_limited(slot)
            # Try again with next key if available
            next_slot = _pool.pick()
            if next_slot is not None:
                logger.info("Retrying with {} after {} hit 429", next_slot.label, slot.label)
                next_slot.last_request_at = time.monotonic()
                next_slot.request_count += 1
                headers["Authorization"] = f"Bearer {next_slot.key}"
                try:
                    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                        resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code == 429:
                        _pool.mark_rate_limited(next_slot)
                        raise GrokError("All Groq keys rate-limited (429)")
                except httpx.RequestError as exc:
                    raise GrokError(f"xAI network error on retry: {exc}") from exc
            else:
                raise GrokError("xAI rate limit exceeded — all keys exhausted")
        if not resp.is_success:
            raise GrokError(f"xAI {resp.status_code}: {resp.text[:300]}")

        try:
            return resp.json()
        except Exception as exc:
            raise GrokError(f"Failed to parse xAI response: {exc}") from exc


def pool_status() -> list[dict[str, Any]]:
    """Return current status of all keys in the pool (for health endpoints)."""
    return _pool.status()
