"""Bitget private WebSocket stream — real-time order fill notifications.

Maintains a persistent authenticated connection to Bitget's private
WebSocket and calls `on_order_fill` the moment a USDT-FUTURES order is
fully filled (SL/TP hit, liquidation, manual close).

One instance per unique API key; shared across all agents using that key.
Auto-reconnects on any error with exponential back-off.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Awaitable, Callable

import websockets
from loguru import logger

_WS_URL = "wss://ws.bitget.com/v2/ws/private"
_FILLED_STATES = {"filled", "full_fill", "partially_fill", "partially_filled"}

# Callback type: receives a normalised fill dict
OnFillCallback = Callable[[dict], Awaitable[None]]


class BitgetPrivateStream:
    """Long-lived private WebSocket connection to Bitget USDT-FUTURES."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        on_order_fill: OnFillCallback,
        key_id: str = "",
    ) -> None:
        self._key = api_key
        self._secret = api_secret.encode()
        self._passphrase = passphrase
        self._on_fill = on_order_fill
        self._key_id = key_id  # for log labels
        self._task: asyncio.Task | None = None
        self._running = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Schedule the stream loop as a background asyncio task."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name=f"bitget_ws_{self._key_id}")
        logger.info("bitget_ws [{}] stream started", self._key_id)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("bitget_ws [{}] stream stopped", self._key_id)

    # ── Internal loop ────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        delay = 1.0
        while self._running:
            try:
                await self._connect()
                delay = 1.0  # successful connection resets back-off
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("bitget_ws [{}] disconnected ({}), retry in {:.0f}s",
                               self._key_id, exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _connect(self) -> None:
        async with websockets.connect(
            _WS_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            logger.debug("bitget_ws [{}] connected", self._key_id)
            await self._authenticate(ws)
            await self._subscribe(ws)
            # Keep-alive: Bitget requires a ping every 30s
            ping_task = asyncio.create_task(self._keep_alive(ws))
            try:
                async for raw in ws:
                    await self._handle(raw)
            finally:
                ping_task.cancel()

    async def _keep_alive(self, ws) -> None:
        while True:
            await asyncio.sleep(25)
            try:
                await ws.send("ping")
            except Exception:
                break

    # ── Auth & subscribe ─────────────────────────────────────────────────────

    def _sign(self, ts: str) -> str:
        prehash = f"{ts}GET/user/verify".encode()
        return base64.b64encode(
            hmac.new(self._secret, prehash, hashlib.sha256).digest()
        ).decode()

    async def _authenticate(self, ws) -> None:
        ts = str(int(time.time()))
        await ws.send(json.dumps({
            "op": "login",
            "args": [{
                "apiKey": self._key,
                "passPhrase": self._passphrase,
                "timestamp": ts,
                "sign": self._sign(ts),
            }],
        }))
        # Wait for login ack (with timeout)
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw) if raw != "pong" else {}
            if msg.get("event") == "login" and msg.get("code") == "0":
                logger.debug("bitget_ws [{}] authenticated", self._key_id)
                return
            if msg.get("event") == "error":
                raise ConnectionError(f"bitget WS auth error: {msg}")
        raise ConnectionError("bitget WS auth: no ack received")

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({
            "op": "subscribe",
            "args": [
                # Real-time order updates (fills, SL/TP hits)
                {"instType": "USDT-FUTURES", "channel": "orders", "instId": "default"},
            ],
        }))
        logger.debug("bitget_ws [{}] subscribed to orders channel", self._key_id)

    # ── Message handling ─────────────────────────────────────────────────────

    async def _handle(self, raw: str) -> None:
        if raw == "pong":
            return
        try:
            msg = json.loads(raw)
        except Exception:
            return

        if msg.get("event") in ("subscribe", "login", "error"):
            if msg.get("event") == "error":
                logger.warning("bitget_ws [{}] server error: {}", self._key_id, msg)
            return

        channel = (msg.get("arg") or {}).get("channel", "")
        if channel != "orders":
            return

        for order in msg.get("data") or []:
            state = str(order.get("state") or order.get("status") or "").lower()
            if state not in _FILLED_STATES:
                continue

            # Normalise symbol: Bitget may send "BTCUSDT" or "BTCUSDT_UMCBL"
            raw_sym = str(order.get("symbol") or order.get("instId") or "")
            symbol = raw_sym.upper().replace("_UMCBL", "").replace("_DMCBL", "")

            fill = {
                "symbol": symbol,
                "order_id": str(order.get("ordId") or order.get("orderId") or ""),
                "client_order_id": str(order.get("clOrdId") or order.get("clientOid") or ""),
                "side": str(order.get("side") or "").lower(),
                "avg_fill_price": float(order.get("avgPx") or order.get("priceAvg") or 0),
                "filled_qty": float(order.get("accFillSz") or order.get("baseVolume") or 0),
                "pnl": float(order.get("pnl") or order.get("profit") or 0),
                "reduce_only": bool(order.get("reduceOnly") or order.get("reduce_only")),
                "closed_at_ms": int(order.get("uTime") or order.get("cTime") or 0),
            }
            logger.info(
                "bitget_ws [{}] ORDER FILLED: {} {} @ {} pnl={}",
                self._key_id, symbol, fill["side"], fill["avg_fill_price"], fill["pnl"],
            )
            try:
                await self._on_fill(fill)
            except Exception as exc:
                logger.exception("bitget_ws [{}] on_fill callback error: {}", self._key_id, exc)
