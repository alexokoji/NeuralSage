"""OpenAI GPT client for premium AI analysis.

Used for critical decision-making: market analysis, fleet insights,
strategy generation, and emergency optimization. Groq handles lighter
tasks (suggestions, chat).

Usage
-----
    from app.services.openai_client import GPTClient
    client = GPTClient()
    text = await client.chat([{"role": "user", "content": "Hello!"}])
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from loguru import logger

from app.config import settings

_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4.1-mini"
_MINI_MODEL = "gpt-4.1-nano"
_TIMEOUT = 45.0
_MIN_REQUEST_INTERVAL = 0.5


class GPTError(Exception):
    """General OpenAI API error."""


class GPTUnavailableError(GPTError):
    """Raised when no OPENAI_API_KEY is configured."""


class GPTClient:
    """Async wrapper around OpenAI's chat-completions endpoint."""

    def __init__(self, model: str | None = None) -> None:
        self._key = getattr(settings, "OPENAI_API_KEY", "") or ""
        if not self._key:
            raise GPTUnavailableError("No OPENAI_API_KEY configured")
        self._model = model or _DEFAULT_MODEL
        self._mini = _MINI_MODEL
        self._last_request_at = 0.0

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
            raise GPTError(f"GPT returned non-JSON: {raw[:200]}") from exc

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
        wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)

        self._last_request_at = time.monotonic()

        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

        url = _BASE_URL + path
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise GPTError(f"OpenAI request timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise GPTError(f"OpenAI network error: {exc}") from exc

        if resp.status_code == 401:
            raise GPTUnavailableError("OpenAI API key is invalid (401)")
        if resp.status_code == 429:
            raise GPTError("OpenAI rate limit exceeded (429)")
        if resp.status_code == 402:
            raise GPTError("OpenAI billing limit reached (402) — add credits at platform.openai.com")
        if not resp.is_success:
            raise GPTError(f"OpenAI {resp.status_code}: {resp.text[:300]}")

        try:
            return resp.json()
        except Exception as exc:
            raise GPTError(f"Failed to parse OpenAI response: {exc}") from exc
