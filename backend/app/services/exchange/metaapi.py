"""MetaAPI cloud bridge for MetaTrader 5 forex trading.

MetaAPI (metaapi.cloud) connects to any MT5 broker account via REST API,
so it works from Render/cloud servers without needing a Windows MT5 terminal.

Free tier: 1 MT5 account, unlimited API calls.

Setup:
1. Open a demo MT5 account with any broker (Exness, XM, IC Markets, etc.)
2. Sign up at metaapi.cloud (free)
3. Connect your MT5 account in MetaAPI dashboard
4. Get your MetaAPI auth token + account ID
5. In NeuralSage Settings: exchange="mt5", API Key=token, API Secret=accountId

When ready for live: connect your real MT5 account in MetaAPI — same API.
"""
from __future__ import annotations

from typing import Any

import httpx
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

_CLIENT_API = "https://mt-client-api-v1.agiliumtrade.agiliumtrade.ai"
_PROVISIONING_API = "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai"
_TIMEOUT = 20.0

_TIMEFRAME_MAP = {
    "1m": "1m",
    "3m": "5m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


class MetaApiClient(ExchangeClient):
    """MetaAPI REST client for MT5 forex trading."""

    name = "mt5"

    def __init__(
        self,
        auth_token: str,
        account_id: str,
        *,
        is_demo: bool = True,
    ) -> None:
        self._auth_token = auth_token
        self._account_id = account_id
        self.is_testnet = is_demo
        self._headers = {
            "auth-token": self._auth_token,
            "Content-Type": "application/json",
        }
        self._client: httpx.AsyncClient | None = None
        self._deployed = False

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self._headers,
                timeout=_TIMEOUT,
            )
        return self._client

    async def _ensure_deployed(self) -> None:
        """Make sure the MT5 account is deployed (started) on MetaAPI servers."""
        if self._deployed:
            return

        client = self._get_client()
        url = f"{_PROVISIONING_API}/users/current/accounts/{self._account_id}"
        try:
            resp = await client.get(url)
            if not resp.is_success:
                raise ExchangeError(f"MetaAPI account check failed: {resp.status_code} {resp.text[:200]}")
            data = resp.json()
            state = data.get("state", "")
            if state == "DEPLOYED":
                self._deployed = True
                return
            if state == "UNDEPLOYED":
                deploy_resp = await client.post(f"{url}/deploy")
                if not deploy_resp.is_success:
                    raise ExchangeError(f"MetaAPI deploy failed: {deploy_resp.text[:200]}")
                logger.info("MetaAPI account {} deploying...", self._account_id)
                # Wait for deployment
                import asyncio
                for _ in range(30):
                    await asyncio.sleep(2)
                    check = await client.get(url)
                    if check.is_success and check.json().get("state") == "DEPLOYED":
                        self._deployed = True
                        return
                raise ExchangeError("MetaAPI account deployment timed out")
        except httpx.RequestError as exc:
            raise ExchangeError(f"MetaAPI network error: {exc}") from exc

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        await self._ensure_deployed()
        client = self._get_client()
        url = f"{_CLIENT_API}{path}"
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ExchangeError(f"MetaAPI request timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise ExchangeError(f"MetaAPI network error: {exc}") from exc

        if resp.status_code == 401:
            raise InsufficientPermissions("MetaAPI auth token is invalid")
        if resp.status_code == 403:
            raise InsufficientPermissions("MetaAPI: insufficient permissions for this account")
        if resp.status_code == 404:
            raise ExchangeError(f"MetaAPI: resource not found — {path}")
        if not resp.is_success:
            try:
                err = resp.json()
                msg = err.get("message", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            raise ExchangeError(f"MetaAPI {resp.status_code}: {msg}")

        return resp.json()

    async def verify_permissions(self) -> list[str]:
        data = await self._request(
            "GET",
            f"/users/current/accounts/{self._account_id}/account-information",
        )
        broker = data.get("broker", "unknown")
        server = data.get("server", "unknown")
        balance = data.get("balance", 0)
        leverage = data.get("leverage", 0)
        logger.info(
            "MT5 account verified: broker={} server={} balance={} leverage=1:{}",
            broker, server, balance, leverage,
        )
        return ["read", "trade"]

    async def get_balances(self) -> list[Balance]:
        data = await self._request(
            "GET",
            f"/users/current/accounts/{self._account_id}/account-information",
        )
        balance = float(data.get("balance", 0))
        equity = float(data.get("equity", 0))
        free_margin = float(data.get("freeMargin", 0))
        currency = data.get("currency", "USD")

        return [
            Balance(
                asset=currency,
                available=free_margin,
                total=equity,
                usd_value=equity,
            )
        ]

    async def get_ticker(self, symbol: str) -> Ticker:
        data = await self._request(
            "GET",
            f"/users/current/accounts/{self._account_id}/symbols/{symbol}/current-price",
        )
        bid = float(data.get("bid", 0))
        ask = float(data.get("ask", 0))
        mid = (bid + ask) / 2

        return Ticker(
            symbol=symbol,
            last=mid,
            bid=bid,
            ask=ask,
            high_24h=float(data.get("high", mid)),
            low_24h=float(data.get("low", mid)),
            volume_24h=0,
            change_24h_pct=0,
        )

    async def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        timeframe = _TIMEFRAME_MAP.get(interval, "15m")

        data = await self._request(
            "GET",
            f"/users/current/accounts/{self._account_id}/historical-market-data/symbols/{symbol}/timeframes/{timeframe}/candles",
            params={"limit": min(limit, 1000)},
        )

        candles: list[Candle] = []
        raw_candles = data if isinstance(data, list) else data.get("candles", [])
        for c in raw_candles:
            time_str = c.get("time", "")
            # MetaAPI returns ISO datetime strings
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                open_time_ms = int(dt.timestamp() * 1000)
            except Exception:
                open_time_ms = 0

            candles.append(Candle(
                open_time=open_time_ms,
                open=float(c.get("open", 0)),
                high=float(c.get("high", 0)),
                low=float(c.get("low", 0)),
                close=float(c.get("close", 0)),
                volume=float(c.get("tickVolume", c.get("volume", 0))),
            ))

        return candles

    async def place_order(self, order: OrderRequest) -> OrderResult:
        if order.side == "buy":
            action_type = "ORDER_TYPE_BUY"
        else:
            action_type = "ORDER_TYPE_SELL"

        # MT5 uses volume in lots (0.01 = micro lot)
        # Convert quantity to lots: for forex, 1 lot = 100,000 units
        volume = order.quantity
        if volume >= 1000:
            volume = round(volume / 100000, 2)
        volume = max(0.01, round(volume, 2))

        trade_body: dict[str, Any] = {
            "actionType": action_type,
            "symbol": order.symbol,
            "volume": volume,
        }

        if order.stop_loss:
            trade_body["stopLoss"] = round(order.stop_loss, 5)
        if order.take_profit:
            trade_body["takeProfit"] = round(order.take_profit, 5)
        if order.client_order_id:
            trade_body["comment"] = order.client_order_id[:26]

        data = await self._request(
            "POST",
            f"/users/current/accounts/{self._account_id}/trade",
            json=trade_body,
        )

        order_id = data.get("orderId", data.get("positionId", ""))
        string_code = data.get("stringCode", "")

        if string_code in ("TRADE_RETCODE_DONE", "TRADE_RETCODE_PLACED"):
            return OrderResult(
                exchange_order_id=str(order_id),
                status="filled",
                avg_fill_price=float(data.get("price", 0)) or None,
                filled_qty=volume,
                raw=data,
            )

        if "ERR" in string_code or "REJECT" in string_code:
            return OrderResult(
                exchange_order_id=str(order_id),
                status="rejected",
                raw=data,
            )

        return OrderResult(
            exchange_order_id=str(order_id),
            status="pending",
            raw=data,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self._request(
                "POST",
                f"/users/current/accounts/{self._account_id}/trade",
                json={
                    "actionType": "ORDER_CANCEL",
                    "orderId": order_id,
                },
            )
            return True
        except ExchangeError:
            return False

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        data = await self._request(
            "GET",
            f"/users/current/accounts/{self._account_id}/positions",
        )

        positions = data if isinstance(data, list) else data.get("positions", [])
        results: list[OrderResult] = []
        for pos in positions:
            pos_symbol = pos.get("symbol", "")
            if symbol and pos_symbol.upper() != symbol.upper():
                continue
            results.append(OrderResult(
                exchange_order_id=str(pos.get("id", "")),
                status="open",
                avg_fill_price=float(pos.get("openPrice", 0)),
                filled_qty=float(pos.get("volume", 0)),
                raw=pos,
            ))

        return results

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
