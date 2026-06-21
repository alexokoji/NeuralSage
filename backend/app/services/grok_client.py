"""Groq Cloud client.

Groq's API is fully OpenAI-compatible. We target it directly over httpx so
we don't need to add the `openai` package as a dependency.

Free tier: sign up at https://console.groq.com — no credit card required.

All methods are async-first and return parsed dicts. They raise
`GrokUnavailableError` when the API key is absent or when Groq returns a
non-2xx that we should propagate, and `GrokError` for all other failures.

Usage
-----
    from app.services.grok_client import GrokClient
    client = GrokClient()
    text = await client.chat([{"role": "user", "content": "Hello!"}])
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
from loguru import logger

from app.config import settings

# Groq free tier: 30 requests/min, 14,400/day.
# Proactive throttle: space requests ~2.5s apart (24 req/min, safely under limit).
# If we still get a 429, back off for 30s (not 60s — faster recovery).
_rate_limited_until: float = 0.0
_RATE_LIMIT_BACKOFF_SECONDS = 30.0
_last_request_at: float = 0.0
_MIN_REQUEST_INTERVAL = 2.5

# Groq Cloud — OpenAI-compatible endpoint
_BASE_URL = "https://api.groq.com/openai/v1"
# Main model: best reasoning on the free tier
_DEFAULT_MODEL = "llama-3.3-70b-versatile"
# Fast/cheap model for per-tick signal validation calls
_MINI_MODEL = "llama-3.1-8b-instant"
_TIMEOUT = 30.0


class GrokError(Exception):
    """General xAI / network error."""


class GrokUnavailableError(GrokError):
    """Raised when no API key is configured — callers should degrade gracefully."""


class GrokClient:
    """Thin async wrapper around xAI's chat-completions endpoint."""

    def __init__(self, model: str = _DEFAULT_MODEL) -> None:
        self._model = model
        self._mini = _MINI_MODEL
        key = getattr(settings, "GROQ_API_KEY", "") or ""
        if not key:
            raise GrokUnavailableError("GROQ_API_KEY is not configured")
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    # Public helpers
    # ------------------------------------------------------------------ #

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        mini: bool = False,
    ) -> str:
        """Send a chat-completions request; return the assistant text."""
        payload = self._build_payload(
            messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            mini=mini,
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
        """Like `chat` but instructs the model to reply with valid JSON and parses it."""
        payload = self._build_payload(
            messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            mini=mini,
            json_mode=True,
        )
        data = await self._post("/chat/completions", payload)
        raw = data["choices"][0]["message"]["content"]
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GrokError(f"Grok returned non-JSON: {raw[:200]}") from exc

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

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
        global _rate_limited_until, _last_request_at
        import asyncio

        # Honour the back-off window set by a previous 429 response.
        remaining = _rate_limited_until - time.monotonic()
        if remaining > 0:
            raise GrokError(
                f"xAI rate limit — backing off for {remaining:.0f}s more"
            )

        # Proactive throttle: space requests apart to avoid hitting 30/min limit.
        wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)

        _last_request_at = time.monotonic()

        url = _BASE_URL + path
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=self._headers)
        except httpx.TimeoutException as exc:
            raise GrokError(f"xAI request timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise GrokError(f"xAI network error: {exc}") from exc

        if resp.status_code == 401:
            raise GrokUnavailableError("XAI_API_KEY is invalid (401)")
        if resp.status_code == 429:
            _rate_limited_until = time.monotonic() + _RATE_LIMIT_BACKOFF_SECONDS
            logger.warning(
                "Groq rate-limited (429) — pausing Grok calls for {}s",
                int(_RATE_LIMIT_BACKOFF_SECONDS),
            )
            raise GrokError("xAI rate limit exceeded (429)")
        if not resp.is_success:
            raise GrokError(f"xAI {resp.status_code}: {resp.text[:300]}")

        try:
            return resp.json()
        except Exception as exc:
            raise GrokError(f"Failed to parse xAI response: {exc}") from exc
