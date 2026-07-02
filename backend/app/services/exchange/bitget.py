"""Bitget v2 unified API client (USDT-FUTURES productType).

Signing:
  Base64(HMAC-SHA256( timestamp + method + requestPath + queryString_or_body, secret ))
Plus passphrase header.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings
from app.services.exchange.base import (
    Balance,
    Candle,
    ExchangeClient,
    ExchangeError,
    InsufficientPermissions,
    OrderRequest,
    OrderResult,
    Ticker,
)

_TIMEFRAME_MAP = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
}


class BitgetClient(ExchangeClient):
    """Bitget unified API.

    Bitget API keys carry a passphrase set at creation time.
    We accept it concatenated as `secret\n<passphrase>`; if no newline,
    we treat the whole string as secret only and require BITGET_PASSPHRASE
    via env (single-tenant deployments only).
    """

    name = "bitget"

    def __init__(self, api_key: str, api_secret: str, *, is_testnet: bool = False) -> None:
        if "\n" in api_secret:
            secret, passphrase = api_secret.split("\n", 1)
        else:
            secret = api_secret
            passphrase = getattr(settings, "BITGET_PASSPHRASE", "") or ""
        self._key = api_key
        self._secret = secret.encode()
        self._passphrase = passphrase
        self.is_testnet = is_testnet
        self._http = httpx.AsyncClient(
            base_url=settings.BITGET_REST_URL, timeout=httpx.Timeout(15.0)
        )

    # -------- signing --------

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        prehash = f"{ts}{method.upper()}{path}{body}".encode()
        digest = hmac.new(self._secret, prehash, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _headers(self, ts: str, sig: str) -> dict[str, str]:
        return {
            "ACCESS-KEY": self._key,
            "ACCESS-SIGN": sig,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        retry=retry_if_exception_type(httpx.TransportError),
    )
    async def _signed(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ts = str(int(time.time() * 1000))
        if method.upper() == "GET":
            qs = ""
            if params:
                # Sort params so the query string in the signature matches
                # exactly what httpx sends — param order matters for Bitget.
                qs = "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            sig = self._sign(ts, method, path + qs, "")
            # Pass the pre-sorted query string directly instead of a dict so
            # httpx cannot reorder the params and break the signature.
            resp = await self._http.get(path + qs, headers=self._headers(ts, sig))
        else:
            payload = json.dumps(body or {}, separators=(",", ":"))
            sig = self._sign(ts, method, path, payload)
            resp = await self._http.request(
                method, path, content=payload, headers=self._headers(ts, sig)
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise ExchangeError(f"bitget non-json response {resp.status_code}") from exc
        if str(data.get("code")) != "00000":
            raise ExchangeError(f"bitget error {data.get('code')}: {data.get('msg')}")
        return data.get("data", {})

    async def _public(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._http.get(path, params=params)
        data = resp.json()
        if str(data.get("code")) != "00000":
            raise ExchangeError(f"bitget public {data.get('code')}: {data.get('msg')}")
        return data.get("data")

    # -------- interface --------

    async def verify_permissions(self) -> list[str]:
        try:
            info = await self._signed("GET", "/api/v2/user/api-keys")
        except ExchangeError as e:
            # Some Bitget account types do not expose the /api/v2/user/api-keys endpoint.
            # If we can call get_balances (authenticated), assume the key has read + trade.
            if "40404" in str(e):
                try:
                    await self.get_balances()
                    return ["read", "trade"]
                except Exception:
                    raise InsufficientPermissions(
                        "bitget key cannot access balances; check permissions"
                    )
            raise
        
        items = info if isinstance(info, list) else [info]
        flags: set[str] = set()
        for k in items:
            if str(k.get("apiKey")) != self._key:
                continue
            perms = (k.get("permList") or k.get("permissions") or [])
            joined = " ".join(perms).lower()
            if "withdraw" in joined or "transfer" in joined:
                raise InsufficientPermissions(
                    "bitget key has withdrawal/transfer permission; revoke and re-issue"
                )
            if "trade" in joined or "contract" in joined:
                flags.add("trade")
            flags.add("read")
        if not flags:
            # Fallback: if the call succeeded the key is at least readable.
            flags = {"read"}
        return sorted(flags)

    async def get_balances(self) -> list[Balance]:
        result = await self._signed(
            "GET",
            "/api/v2/mix/account/accounts",
            params={"productType": "USDT-FUTURES"},
        )
        items = result if isinstance(result, list) else []
        out: list[Balance] = []
        for r in items:
            total = float(r.get("equity") or r.get("usdtEquity") or 0)
            avail = float(r.get("available") or r.get("crossedMaxAvailable") or total)
            if total > 0:
                out.append(
                    Balance(
                        asset=r.get("marginCoin") or "USDT",
                        available=avail,
                        total=total,
                        usd_value=total if (r.get("marginCoin") or "USDT") == "USDT" else None,
                    )
                )
        return out

    async def get_ticker(self, symbol: str) -> Ticker:
        data = await self._public(
            "/api/v2/mix/market/ticker",
            {"productType": "USDT-FUTURES", "symbol": symbol},
        )
        items = data if isinstance(data, list) else [data]
        if not items:
            raise ExchangeError(f"bitget: no ticker for {symbol}")
        t = items[0]
        last = float(t["lastPr"])
        return Ticker(
            symbol=t["symbol"],
            last=last,
            bid=float(t.get("bidPr") or last),
            ask=float(t.get("askPr") or last),
            high_24h=float(t["high24h"]),
            low_24h=float(t["low24h"]),
            volume_24h=float(t["baseVolume"]),
            change_24h_pct=float(t.get("change24h") or 0) * 100,
        )

    async def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        granularity = _TIMEFRAME_MAP.get(interval, interval)
        data = await self._public(
            "/api/v2/mix/market/candles",
            {
                "productType": "USDT-FUTURES",
                "symbol": symbol,
                "granularity": granularity,
                "limit": str(limit),
            },
        )
        rows = data or []
        return [
            Candle(
                open_time=int(r[0]),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
            )
            for r in rows
        ]

    async def place_order(self, order: OrderRequest) -> OrderResult:
        body: dict[str, Any] = {
            "productType": "USDT-FUTURES",
            "marginCoin": "USDT",
            "symbol": order.symbol,
            "marginMode": "crossed",
            "side": order.side,
            "orderType": order.order_type,
            "size": str(order.quantity),
            "tradeSide": "open" if not order.reduce_only else "close",
        }
        if order.price is not None and order.order_type == "limit":
            body["price"] = str(order.price)
        if order.stop_loss is not None:
            body["presetStopLossPrice"] = str(order.stop_loss)
        if order.take_profit is not None:
            body["presetStopSurplusPrice"] = str(order.take_profit)
        if order.client_order_id:
            body["clientOid"] = order.client_order_id

        result = await self._signed("POST", "/api/v2/mix/order/place-order", body=body)
        return OrderResult(
            exchange_order_id=str(result.get("orderId")),
            status="pending",
            raw=result,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        await self._signed(
            "POST",
            "/api/v2/mix/order/cancel-order",
            body={
                "productType": "USDT-FUTURES",
                "marginCoin": "USDT",
                "symbol": symbol,
                "orderId": order_id,
            },
        )
        return True

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        params: dict[str, Any] = {"productType": "USDT-FUTURES"}
        if symbol:
            params["symbol"] = symbol
        data = await self._signed("GET", "/api/v2/mix/order/orders-pending", params=params)
        rows = data.get("entrustedList") if isinstance(data, dict) else (data or [])
        out: list[OrderResult] = []
        for o in rows or []:
            status_raw = (o.get("status") or "").lower()
            mapped = {
                "live": "open",
                "new": "open",
                "partial_fill": "open",
                "filled": "filled",
                "cancelled": "cancelled",
                "canceled": "cancelled",
            }.get(status_raw, "pending")
            out.append(
                OrderResult(
                    exchange_order_id=o["orderId"],
                    status=mapped,  # type: ignore[arg-type]
                    avg_fill_price=float(o.get("priceAvg") or 0) or None,
                    filled_qty=float(o.get("baseVolume") or 0),
                    raw=o,
                )
            )
        return out

    async def set_leverage(self, symbol: str, leverage: int, side: str = "long") -> None:
        """Set futures leverage for a symbol before placing an order.

        Bitget hedge mode requires holdSide; one-way mode rejects it.
        Try hedge mode first, fall back to one-way (no holdSide).
        """
        base = {
            "productType": "USDT-FUTURES",
            "marginCoin": "USDT",
            "symbol": symbol,
            "leverage": str(leverage),
        }
        # Hedge mode: set leverage for both sides.
        for hold_side in (side, "long" if side == "short" else "short"):
            try:
                await self._signed("POST", "/api/v2/mix/account/set-leverage",
                                   body={**base, "holdSide": hold_side})
            except Exception:
                pass
        # One-way mode fallback: no holdSide.
        try:
            await self._signed("POST", "/api/v2/mix/account/set-leverage", body=base)
            logger.info("bitget set_leverage {}/{}x ok", symbol, leverage)
        except Exception as exc:
            logger.info("bitget set_leverage {}/{}x: {}", symbol, leverage, exc)

    async def get_positions(self) -> list[dict[str, Any]]:
        """Return currently open USDT-FUTURES positions from Bitget."""
        try:
            data = await self._signed(
                "GET",
                "/api/v2/mix/position/all-position",
                params={"productType": "USDT-FUTURES", "marginCoin": "USDT"},
            )
            rows = data if isinstance(data, list) else (data.get("data") or [])
        except Exception:
            raise
        out = []
        for item in rows or []:
            total = float(item.get("total") or item.get("openSizeLeft") or 0)
            if total <= 0:
                continue
            # Bitget symbol format is e.g. "BTCUSDT"; normalise to uppercase
            sym = str(item.get("symbol") or "").upper().replace("_UMCBL", "").replace("_DMCBL", "")
            out.append({
                "symbol": sym,
                "side": str(item.get("holdSide") or "").lower(),
                "qty": total,
                "entry_price": float(item.get("averageOpenPrice") or item.get("openPriceAvg") or 0),
                "mark_price": float(item.get("markPrice") or 0),
                "unrealized_pnl": float(item.get("unrealizedPL") or 0),
            })
        return out

    async def get_closed_orders(
        self,
        symbol: str | None = None,
        limit: int = 100,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return recently filled/cancelled orders from Bitget USDT-FUTURES.

        Each returned dict contains:
          symbol, order_id, side, avg_fill_price, filled_qty, pnl, status, closed_at_ms
        """
        params: dict[str, Any] = {
            "productType": "USDT-FUTURES",
            "limit": str(limit),
        }
        if symbol:
            params["symbol"] = symbol
        if start_ms:
            params["startTime"] = str(start_ms)
        if end_ms:
            params["endTime"] = str(end_ms)

        try:
            data = await self._signed("GET", "/api/v2/mix/order/orders-history", params=params)
        except Exception:
            raise

        rows = (
            data.get("entrustedList")
            or data.get("orderList")
            or (data if isinstance(data, list) else [])
        )
        out = []
        for o in rows or []:
            status_raw = str(o.get("state") or o.get("status") or "").lower()
            # Only return filled orders (fully or partially)
            if status_raw not in ("filled", "full_fill", "partially_fill", "partially_filled"):
                continue
            sym = str(o.get("symbol") or "").upper().replace("_UMCBL", "").replace("_DMCBL", "")
            out.append({
                "symbol": sym,
                "order_id": str(o.get("orderId") or ""),
                "client_order_id": str(o.get("clientOid") or ""),
                "side": str(o.get("side") or "").lower(),
                "avg_fill_price": float(o.get("priceAvg") or o.get("price") or 0),
                "filled_qty": float(o.get("baseVolume") or o.get("filledQty") or 0),
                "pnl": float(o.get("profit") or o.get("realizedPnl") or 0),
                "status": status_raw,
                "closed_at_ms": int(o.get("uTime") or o.get("cTime") or 0),
            })
        return out

    async def close(self) -> None:
        await self._http.aclose()
