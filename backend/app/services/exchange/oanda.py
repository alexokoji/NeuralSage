"""OANDA v20 REST API client for forex trading.

Supports both practice (paper) and live accounts.
Practice: https://api-fxpractice.oanda.com
Live:     https://api-fxtrade.oanda.com

Authentication is via Bearer token (API key generated in OANDA portal).
No HMAC signing needed — simpler than crypto exchanges.

Forex conventions:
- Instruments use underscore format: EUR_USD, GBP_JPY, USD_JPY
- Prices are quoted in pips (0.0001 for most pairs, 0.01 for JPY pairs)
- Position sizes are in "units" (1 unit = 1 of base currency)
- Leverage is handled server-side by OANDA based on account settings
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx
from loguru import logger

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

_TIMEOUT = 15.0

_TIMEFRAME_MAP = {
    "1m": "M1",
    "3m": "M3",  # OANDA doesn't have 3m — we'll map to M5
    "5m": "M5",
    "15m": "M15",
    "30m": "M30",
    "1h": "H1",
    "4h": "H4",
    "1d": "D",
}

# Common forex pairs with pip locations
_JPY_PAIRS = {"USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY", "CAD_JPY", "CHF_JPY", "NZD_JPY"}


def _symbol_to_oanda(symbol: str) -> str:
    """Convert NeuralSage symbol format to OANDA instrument format.

    EURUSD -> EUR_USD, GBPJPY -> GBP_JPY
    """
    s = symbol.upper().replace("_", "").replace("/", "")
    if len(s) == 6:
        return f"{s[:3]}_{s[3:]}"
    return symbol


def _oanda_to_symbol(instrument: str) -> str:
    """Convert OANDA instrument format to NeuralSage format.

    EUR_USD -> EURUSD
    """
    return instrument.replace("_", "")


class OandaClient(ExchangeClient):
    """OANDA v20 REST API client."""

    name = "oanda"

    def __init__(
        self,
        api_key: str,
        account_id: str,
        *,
        is_practice: bool = True,
    ) -> None:
        self._api_key = api_key
        self._account_id = account_id
        self.is_testnet = is_practice

        if is_practice:
            self._base_url = settings.OANDA_REST_URL or "https://api-fxpractice.oanda.com"
        else:
            self._base_url = "https://api-fxtrade.oanda.com"

        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "UNIX",
        }
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=_TIMEOUT,
            )
        return self._client

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        client = self._get_client()
        try:
            resp = await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ExchangeError(f"OANDA request timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise ExchangeError(f"OANDA network error: {exc}") from exc

        if resp.status_code == 401:
            raise InsufficientPermissions("OANDA API key is invalid or expired")
        if resp.status_code == 403:
            raise InsufficientPermissions("OANDA API key lacks required permissions")
        if not resp.is_success:
            try:
                err = resp.json()
                msg = err.get("errorMessage", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise ExchangeError(f"OANDA {resp.status_code}: {msg}")

        return resp.json()

    async def verify_permissions(self) -> list[str]:
        data = await self._request("GET", f"/v3/accounts/{self._account_id}")
        account = data.get("account", {})
        permissions = ["read", "trade"]
        financing = account.get("financing", "")
        logger.info(
            "OANDA account verified: {} (balance: {})",
            self._account_id, account.get("balance"),
        )
        return permissions

    async def get_balances(self) -> list[Balance]:
        data = await self._request("GET", f"/v3/accounts/{self._account_id}/summary")
        account = data.get("account", {})
        balance = float(account.get("balance", 0))
        unrealized = float(account.get("unrealizedPL", 0))
        margin_available = float(account.get("marginAvailable", 0))
        currency = account.get("currency", "USD")

        return [
            Balance(
                asset=currency,
                available=margin_available,
                total=balance + unrealized,
                usd_value=balance + unrealized,
            )
        ]

    async def get_ticker(self, symbol: str) -> Ticker:
        instrument = _symbol_to_oanda(symbol)
        data = await self._request(
            "GET",
            f"/v3/accounts/{self._account_id}/pricing",
            params={"instruments": instrument},
        )
        prices = data.get("prices", [])
        if not prices:
            raise ExchangeError(f"No pricing data for {instrument}")

        p = prices[0]
        bids = p.get("bids", [{}])
        asks = p.get("asks", [{}])
        bid = float(bids[0].get("price", 0)) if bids else 0
        ask = float(asks[0].get("price", 0)) if asks else 0
        mid = (bid + ask) / 2

        return Ticker(
            symbol=symbol,
            last=mid,
            bid=bid,
            ask=ask,
            high_24h=mid,
            low_24h=mid,
            volume_24h=0,
            change_24h_pct=0,
        )

    async def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        instrument = _symbol_to_oanda(symbol)
        granularity = _TIMEFRAME_MAP.get(interval, "M15")
        # OANDA caps at 5000 candles per request
        count = min(limit, 5000)

        data = await self._request(
            "GET",
            f"/v3/instruments/{instrument}/candles",
            params={
                "granularity": granularity,
                "count": count,
                "price": "M",  # mid prices
            },
        )

        candles: list[Candle] = []
        for c in data.get("candles", []):
            if not c.get("complete", False) and len(candles) > 0:
                continue
            mid = c.get("mid", {})
            candles.append(Candle(
                open_time=int(float(c.get("time", 0)) * 1000),
                open=float(mid.get("o", 0)),
                high=float(mid.get("h", 0)),
                low=float(mid.get("l", 0)),
                close=float(mid.get("c", 0)),
                volume=int(c.get("volume", 0)),
            ))

        return candles

    async def place_order(self, order: OrderRequest) -> OrderResult:
        instrument = _symbol_to_oanda(order.symbol)
        units = order.quantity
        if order.side == "sell":
            units = -units

        order_body: dict[str, Any] = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(int(units)),
                "timeInForce": "FOK",
            }
        }

        if order.stop_loss:
            order_body["order"]["stopLossOnFill"] = {
                "price": f"{order.stop_loss:.5f}",
            }
        if order.take_profit:
            order_body["order"]["takeProfitOnFill"] = {
                "price": f"{order.take_profit:.5f}",
            }

        if order.reduce_only:
            # For closing, use the opposite direction with "reduce" semantics
            # OANDA handles this via positive/negative units
            pass

        if order.client_order_id:
            oid = order.client_order_id.replace("-", "")[:20]
            order_body["order"]["clientExtensions"] = {"id": oid}

        data = await self._request(
            "POST",
            f"/v3/accounts/{self._account_id}/orders",
            json=order_body,
        )

        # Parse the response
        fill = data.get("orderFillTransaction", {})
        cancel = data.get("orderCancelTransaction", {})

        if cancel:
            reason = cancel.get("reason", "unknown")
            return OrderResult(
                exchange_order_id=cancel.get("id", ""),
                status="rejected",
                raw=data,
            )

        if fill:
            return OrderResult(
                exchange_order_id=fill.get("id", ""),
                status="filled",
                avg_fill_price=float(fill.get("price", 0)),
                filled_qty=abs(float(fill.get("units", 0))),
                raw=data,
            )

        # Order created but not yet filled
        order_create = data.get("orderCreateTransaction", {})
        return OrderResult(
            exchange_order_id=order_create.get("id", ""),
            status="pending",
            raw=data,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self._request(
                "PUT",
                f"/v3/accounts/{self._account_id}/orders/{order_id}/cancel",
            )
            return True
        except ExchangeError:
            return False

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        params: dict[str, str] = {}
        if symbol:
            params["instrument"] = _symbol_to_oanda(symbol)

        data = await self._request(
            "GET",
            f"/v3/accounts/{self._account_id}/openTrades",
            params=params if params else None,
        )

        results: list[OrderResult] = []
        for trade in data.get("trades", []):
            instrument = trade.get("instrument", "")
            trade_symbol = _oanda_to_symbol(instrument)
            if symbol and trade_symbol != symbol.upper():
                continue
            results.append(OrderResult(
                exchange_order_id=trade.get("id", ""),
                status="open",
                avg_fill_price=float(trade.get("price", 0)),
                filled_qty=abs(float(trade.get("currentUnits", 0))),
                raw=trade,
            ))

        return results

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
