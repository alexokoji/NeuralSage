"""Deriv WebSocket API client for forex trading.

Deriv uses a WebSocket JSON-RPC API. Since our trading loop runs every
30s, we open a connection per request batch rather than maintaining a
persistent connection.

Setup:
1. Create free account at app.deriv.com (choose Virtual Money for demo)
2. Go to Settings → API Tokens → create token with Trade + Read scope
3. In NeuralSage Settings: exchange="deriv", API Key=token, API Secret=your app_id

Demo uses the same endpoint as live — the account type determines demo/live.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

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

_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id={app_id}"
_DEFAULT_APP_ID = "1089"  # Deriv's default/public app_id
_TIMEOUT = 15.0

_GRANULARITY_MAP = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

# Map NeuralSage forex symbols to Deriv symbols
_SYMBOL_MAP = {
    "EURUSD": "frxEURUSD",
    "GBPUSD": "frxGBPUSD",
    "USDJPY": "frxUSDJPY",
    "AUDUSD": "frxAUDUSD",
    "USDCAD": "frxUSDCAD",
    "USDCHF": "frxUSDCHF",
    "NZDUSD": "frxNZDUSD",
    "GBPJPY": "frxGBPJPY",
    "EURJPY": "frxEURJPY",
    "EURGBP": "frxEURGBP",
    "AUDJPY": "frxAUDJPY",
    "EURAUD": "frxEURAUD",
    "GBPAUD": "frxGBPAUD",
    "XAUUSD": "frxXAUUSD",
    "XAGUSD": "frxXAGUSD",
}

_REVERSE_SYMBOL_MAP = {v: k for k, v in _SYMBOL_MAP.items()}


def _to_deriv(symbol: str) -> str:
    s = symbol.upper().replace("/", "").replace("_", "")
    return _SYMBOL_MAP.get(s, f"frx{s}")


def _from_deriv(deriv_symbol: str) -> str:
    return _REVERSE_SYMBOL_MAP.get(deriv_symbol, deriv_symbol.replace("frx", ""))


class DerivClient(ExchangeClient):
    """Deriv WebSocket API client for forex CFD trading."""

    name = "deriv"

    def __init__(
        self,
        api_token: str,
        app_id: str = "",
        *,
        is_demo: bool = True,
    ) -> None:
        self._api_token = api_token
        self._app_id = app_id.strip() or _DEFAULT_APP_ID
        self.is_testnet = is_demo
        self._authorized = False

    async def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Open a WebSocket, send one request, return the response."""
        try:
            import websockets
        except ImportError:
            raise ExchangeError("websockets package not installed — add it to requirements.txt")

        url = _WS_URL.format(app_id=self._app_id)
        try:
            async with websockets.connect(url, close_timeout=5) as ws:
                # Authorize first if needed
                if not payload.get("_skip_auth"):
                    auth_msg = {"authorize": self._api_token}
                    await ws.send(json.dumps(auth_msg))
                    auth_resp = json.loads(await asyncio.wait_for(ws.recv(), _TIMEOUT))
                    if auth_resp.get("error"):
                        err = auth_resp["error"]
                        raise InsufficientPermissions(
                            f"Deriv auth failed: {err.get('message', err)}"
                        )

                # Remove internal flag before sending
                payload.pop("_skip_auth", None)
                await ws.send(json.dumps(payload))
                resp = json.loads(await asyncio.wait_for(ws.recv(), _TIMEOUT))

                if resp.get("error"):
                    err = resp["error"]
                    raise ExchangeError(f"Deriv: {err.get('message', err)}")

                return resp
        except asyncio.TimeoutError:
            raise ExchangeError("Deriv WebSocket request timed out")
        except ConnectionRefusedError as exc:
            raise ExchangeError(f"Deriv connection refused: {exc}") from exc
        except Exception as exc:
            if isinstance(exc, (ExchangeError, InsufficientPermissions)):
                raise
            raise ExchangeError(f"Deriv WebSocket error: {exc}") from exc

    async def _send_public(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a request without authorization (public data)."""
        payload["_skip_auth"] = True
        return await self._send(payload)

    async def verify_permissions(self) -> list[str]:
        resp = await self._send({"authorize": self._api_token})
        # The authorize response itself contains account info
        auth = resp.get("authorize", {})
        account_type = auth.get("account_type", "")
        balance = auth.get("balance", 0)
        currency = auth.get("currency", "USD")
        logger.info(
            "Deriv account verified: type={} balance={} {}",
            account_type, balance, currency,
        )
        return ["read", "trade"]

    async def get_balances(self) -> list[Balance]:
        resp = await self._send({"balance": 1})
        bal = resp.get("balance", {})
        return [
            Balance(
                asset=bal.get("currency", "USD"),
                available=float(bal.get("balance", 0)),
                total=float(bal.get("balance", 0)),
                usd_value=float(bal.get("balance", 0)),
            )
        ]

    async def get_ticker(self, symbol: str) -> Ticker:
        deriv_sym = _to_deriv(symbol)
        resp = await self._send_public({
            "ticks": deriv_sym,
        })
        tick = resp.get("tick", {})
        price = float(tick.get("quote", 0))
        return Ticker(
            symbol=symbol,
            last=price,
            bid=price,
            ask=price,
            high_24h=price,
            low_24h=price,
            volume_24h=0,
            change_24h_pct=0,
        )

    async def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        deriv_sym = _to_deriv(symbol)
        granularity = _GRANULARITY_MAP.get(interval, 900)
        count = min(limit, 5000)

        resp = await self._send_public({
            "ticks_history": deriv_sym,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "granularity": granularity,
            "style": "candles",
        })

        candles: list[Candle] = []
        for c in resp.get("candles", []):
            candles.append(Candle(
                open_time=int(c.get("epoch", 0)) * 1000,
                open=float(c.get("open", 0)),
                high=float(c.get("high", 0)),
                low=float(c.get("low", 0)),
                close=float(c.get("close", 0)),
                volume=0,
            ))

        return candles

    async def place_order(self, order: OrderRequest) -> OrderResult:
        deriv_sym = _to_deriv(order.symbol)

        # Deriv uses "multiplier" contracts for forex CFD-style trading
        buy_payload: dict[str, Any] = {
            "buy": 1,
            "parameters": {
                "contract_type": "MULTUP" if order.side == "buy" else "MULTDOWN",
                "symbol": deriv_sym,
                "currency": "USD",
                "amount": round(order.quantity, 2),
                "multiplier": 100,
            },
        }

        if order.stop_loss:
            buy_payload["parameters"]["stop_loss"] = round(order.stop_loss, 5)
        if order.take_profit:
            buy_payload["parameters"]["take_profit"] = round(order.take_profit, 5)

        resp = await self._send(buy_payload)
        buy = resp.get("buy", {})

        if buy:
            return OrderResult(
                exchange_order_id=str(buy.get("contract_id", "")),
                status="filled",
                avg_fill_price=float(buy.get("buy_price", 0)),
                filled_qty=order.quantity,
                raw=resp,
            )

        return OrderResult(
            exchange_order_id="",
            status="rejected",
            raw=resp,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self._send({
                "sell": int(order_id),
                "price": 0,
            })
            return True
        except ExchangeError:
            return False

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        resp = await self._send({
            "portfolio": 1,
        })

        contracts = resp.get("portfolio", {}).get("contracts", [])
        results: list[OrderResult] = []
        for c in contracts:
            contract_symbol = _from_deriv(c.get("symbol", ""))
            if symbol and contract_symbol.upper() != symbol.upper():
                continue
            results.append(OrderResult(
                exchange_order_id=str(c.get("contract_id", "")),
                status="open",
                avg_fill_price=float(c.get("buy_price", 0)),
                filled_qty=float(c.get("purchase_time", 0)),
                raw=c,
            ))

        return results

    async def close(self) -> None:
        pass
