"""Bybit v5 unified-account REST client (linear/USDT perps + spot).

Signing:
  HMAC-SHA256( timestamp + api_key + recv_window + queryString_or_body )

Public market data (candles, tickers) cascades through three providers:
  1. Bybit mainnet — may be blocked by Cloudflare on cloud-hosted servers.
  2. OKX public spot — generally unrestricted from any IP.
  3. (error) — surface the last failure to the caller.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx
from loguru import logger
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

_RECV_WINDOW = "5000"
_OKX_URL = "https://www.okx.com"

_TIMEFRAME_MAP = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}

# OKX uses uppercase for hours/days
_OKX_INTERVAL_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D",
}


# Symbols that OKX lists under a different name than the Bybit convention.
# Bybit symbol → OKX instId
_OKX_SYMBOL_OVERRIDES: dict[str, str] = {
    "MATICUSDT": "POL-USDT",   # MATIC was rebranded to POL on OKX
    "TONUSDT": "TON-USDT",     # some OKX regions list as TON-USDT, others don't — skip via error
}


def _to_okx_symbol(symbol: str) -> str:
    """BTCUSDT → BTC-USDT, with known rebranding overrides applied."""
    if symbol.upper() in _OKX_SYMBOL_OVERRIDES:
        return _OKX_SYMBOL_OVERRIDES[symbol.upper()]
    s = symbol.upper()
    if s.endswith("USDT"):
        return s[:-4] + "-USDT"
    return s


class BybitClient(ExchangeClient):
    name = "bybit"

    def __init__(self, api_key: str, api_secret: str, *, is_testnet: bool = False) -> None:
        self._key = api_key
        self._secret = api_secret.encode()
        self.is_testnet = is_testnet

        # Route testnet signed requests through a Cloudflare Worker proxy when
        # BYBIT_TESTNET_PROXY_URL is set. The Worker forwards to api-testnet.bybit.com
        # using Cloudflare edge IPs, bypassing the WAF block on cloud-hosting IPs.
        if is_testnet and settings.BYBIT_TESTNET_PROXY_URL:
            signed_base = settings.BYBIT_TESTNET_PROXY_URL
            self._using_proxy = True
        else:
            signed_base = settings.BYBIT_TESTNET_REST_URL if is_testnet else settings.BYBIT_REST_URL
            self._using_proxy = False

        signed_headers: dict[str, str] = {"User-Agent": "NeuralSage/1.0"}
        if self._using_proxy and settings.BYBIT_PROXY_SECRET:
            signed_headers["X-Proxy-Secret"] = settings.BYBIT_PROXY_SECRET

        _pub_headers = {"User-Agent": "NeuralSage/1.0"}
        # Signed requests (orders, balance, verify) go to testnet/proxy when is_testnet.
        self._http = httpx.AsyncClient(base_url=signed_base, timeout=httpx.Timeout(15.0), headers=signed_headers)
        # When the proxy is in use, keep a direct-testnet client as a fallback for when
        # the proxy returns 403. Bybit testnet is often reachable directly even from cloud IPs.
        self._direct_http: httpx.AsyncClient | None = (
            httpx.AsyncClient(
                base_url=settings.BYBIT_TESTNET_REST_URL,
                timeout=httpx.Timeout(15.0),
                headers={"User-Agent": "NeuralSage/1.0"},
            )
            if self._using_proxy
            else None
        )
        # Public market data always tries mainnet first.
        self._pub_http = httpx.AsyncClient(
            base_url=settings.BYBIT_REST_URL, timeout=httpx.Timeout(15.0), headers=_pub_headers
        )
        # OKX public spot — fallback when Bybit is blocked by Cloudflare on cloud IPs.
        self._okx_http = httpx.AsyncClient(
            base_url=_OKX_URL, timeout=httpx.Timeout(15.0), headers=_pub_headers
        )

    # -------- signing --------

    def _sign(self, ts: str, payload: str) -> str:
        msg = f"{ts}{self._key}{_RECV_WINDOW}{payload}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def _headers(self, ts: str, signature: str) -> dict[str, str]:
        return {
            "X-BAPI-API-KEY": self._key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
            "Content-Type": "application/json",
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
            query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
            sig = self._sign(ts, query)
            resp = await self._http.get(path, params=params, headers=self._headers(ts, sig))
        else:
            payload = json.dumps(body or {}, separators=(",", ":"))
            sig = self._sign(ts, payload)
            resp = await self._http.request(
                method, path, content=payload, headers=self._headers(ts, sig)
            )
        try:
            data = resp.json()
        except Exception as exc:
            if resp.status_code == 403:
                if self.is_testnet and self._using_proxy and self._direct_http is not None:
                    # Proxy is broken — try going straight to api-testnet.bybit.com.
                    # Testnet often doesn't enforce the same cloud-IP WAF as mainnet.
                    logger.warning(
                        "bybit proxy returned 403 — body={!r} — retrying directly on api-testnet.bybit.com",
                        resp.text[:120],
                    )
                    ts2 = str(int(time.time() * 1000))
                    if method.upper() == "GET":
                        query2 = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
                        sig2 = self._sign(ts2, query2)
                        resp2 = await self._direct_http.get(path, params=params, headers=self._headers(ts2, sig2))
                    else:
                        payload2 = json.dumps(body or {}, separators=(",", ":"))
                        sig2 = self._sign(ts2, payload2)
                        resp2 = await self._direct_http.request(
                            method, path, content=payload2, headers=self._headers(ts2, sig2)
                        )
                    try:
                        data = resp2.json()
                    except Exception as exc2:
                        raise ExchangeError(
                            f"bybit testnet unreachable via proxy (403) and directly ({resp2.status_code}). "
                            "Check BYBIT_TESTNET_PROXY_URL in Render env vars or remove it to use direct testnet."
                        ) from exc2
                    if data.get("retCode") not in (0, "0"):
                        raise ExchangeError(f"bybit error {data.get('retCode')}: {data.get('retMsg')}")
                    return data.get("result") or {}
                if self.is_testnet:
                    raise ExchangeError(
                        "bybit testnet blocks cloud hosting IPs (Render/AWS) at the CDN level. "
                        "Set BYBIT_TESTNET_PROXY_URL in Render env vars to route through a "
                        "Cloudflare Worker — see Settings → API Keys for setup instructions."
                    ) from exc
                raise ExchangeError(
                    "bybit mainnet 403 — open Bybit → API Management → edit this key → "
                    "make sure 'IP restriction' is set to 'No restriction' (or add this "
                    "server's outbound IP to the whitelist)."
                ) from exc
            raise ExchangeError(f"bybit: non-json response {resp.status_code}") from exc
        if data.get("retCode") not in (0, "0"):
            raise ExchangeError(f"bybit error {data.get('retCode')}: {data.get('retMsg')}")
        return data.get("result") or {}

    async def _public(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._pub_http.get(path, params=params)
        try:
            data = resp.json()
        except Exception as exc:
            raise ExchangeError(f"bybit: non-json public response {resp.status_code}") from exc
        if data.get("retCode") not in (0, "0"):
            raise ExchangeError(f"bybit public error {data.get('retCode')}: {data.get('retMsg')}")
        return data.get("result") or {}

    # -------- OKX fallback (public, no IP restrictions) --------

    async def _okx_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        inst_id = _to_okx_symbol(symbol)
        bar = _OKX_INTERVAL_MAP.get(interval, "15m")
        resp = await self._okx_http.get(
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": min(limit, 300)},
        )
        try:
            data = resp.json()
        except Exception as exc:
            raise ExchangeError(f"okx: non-json candles response {resp.status_code}") from exc
        if data.get("code") != "0":
            raise ExchangeError(f"okx candles error {data.get('code')}: {data.get('msg', data)}")
        rows = data.get("data") or []
        # OKX returns newest-first; each row: [ts, open, high, low, close, vol, ...]
        rows = list(reversed(rows))
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

    async def _okx_ticker(self, symbol: str) -> Ticker:
        inst_id = _to_okx_symbol(symbol)
        resp = await self._okx_http.get("/api/v5/market/ticker", params={"instId": inst_id})
        try:
            data = resp.json()
        except Exception as exc:
            raise ExchangeError(f"okx: non-json ticker response {resp.status_code}") from exc
        if data.get("code") != "0":
            raise ExchangeError(f"okx ticker error {data.get('code')}: {data.get('msg', data)}")
        items = data.get("data") or []
        if not items:
            raise ExchangeError(f"okx: no ticker for {symbol}")
        t = items[0]
        last = float(t["last"])
        open24 = float(t.get("open24h") or last)
        change_pct = ((last - open24) / open24 * 100) if open24 else 0.0
        return Ticker(
            symbol=symbol,
            last=last,
            bid=float(t.get("bidPx") or last),
            ask=float(t.get("askPx") or last),
            high_24h=float(t.get("high24h") or last),
            low_24h=float(t.get("low24h") or last),
            volume_24h=float(t.get("volCcy24h") or 0),
            change_24h_pct=change_pct,
        )

    # -------- interface --------

    async def verify_permissions(self) -> list[str]:
        info = await self._signed("GET", "/v5/user/query-api")
        permissions = info.get("permissions") or {}
        # Bybit returns: {"ContractTrade": [...], "Spot": [...], "Wallet": [...], "Options": [...]}
        flags: list[str] = []
        if any(permissions.get(k) for k in ("ContractTrade", "Spot", "Derivatives")):
            flags.append("trade")
        if permissions.get("Wallet"):
            # "Wallet" can include withdrawal — must inspect grants.
            wallet_grants = permissions.get("Wallet") or []
            if any("Withdraw" in g for g in wallet_grants):
                raise InsufficientPermissions(
                    "bybit key has withdrawal permission; revoke and re-issue with read+trade only"
                )
        # Read is implicit on /v5/user/query-api success.
        flags.append("read")
        # API will refuse the call entirely if the key lacks read.
        return sorted(set(flags))

    async def get_balances(self) -> list[Balance]:
        # Try account types in order: UNIFIED (new accounts) → CONTRACT (older/testnet) → SPOT
        for acct_type in ("UNIFIED", "CONTRACT", "SPOT"):
            try:
                result = await self._signed(
                    "GET", "/v5/account/wallet-balance", params={"accountType": acct_type}
                )
            except ExchangeError:
                continue
            out: list[Balance] = []
            for acct in result.get("list", []):
                for coin in acct.get("coin", []):
                    free = float(coin.get("availableToWithdraw") or coin.get("walletBalance") or 0)
                    total = float(coin.get("walletBalance") or 0)
                    usd = float(coin.get("usdValue") or 0) or None
                    if total > 0:
                        out.append(Balance(asset=coin["coin"], available=free, total=total, usd_value=usd))
            if out:
                return out
        return []

    async def get_ticker(self, symbol: str) -> Ticker:
        try:
            result = await self._public("/v5/market/tickers", {"category": "linear", "symbol": symbol})
            items = result.get("list") or []
            if not items:
                raise ExchangeError(f"bybit: no ticker for {symbol}")
            t = items[0]
            return Ticker(
                symbol=t["symbol"],
                last=float(t["lastPrice"]),
                bid=float(t.get("bid1Price") or t["lastPrice"]),
                ask=float(t.get("ask1Price") or t["lastPrice"]),
                high_24h=float(t["highPrice24h"]),
                low_24h=float(t["lowPrice24h"]),
                volume_24h=float(t["turnover24h"]),
                change_24h_pct=float(t["price24hPcnt"]) * 100,
            )
        except ExchangeError:
            logger.debug("bybit public ticker blocked — falling back to okx for {}", symbol)
            return await self._okx_ticker(symbol)

    async def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        bybit_interval = _TIMEFRAME_MAP.get(interval, interval)
        try:
            result = await self._public(
                "/v5/market/kline",
                {"category": "linear", "symbol": symbol, "interval": bybit_interval, "limit": limit},
            )
            rows = result.get("list") or []
            # Bybit returns newest-first.
            rows = list(reversed(rows))
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
        except ExchangeError:
            logger.debug("bybit public candles blocked — falling back to okx for {}", symbol)
            return await self._okx_candles(symbol, interval, limit)

    async def place_order(self, order: OrderRequest) -> OrderResult:
        body: dict[str, Any] = {
            "category": "linear",
            "symbol": order.symbol,
            "side": "Buy" if order.side == "buy" else "Sell",
            "orderType": "Market" if order.order_type == "market" else "Limit",
            "qty": str(order.quantity),
            "timeInForce": "IOC" if order.order_type == "market" else "GTC",
        }
        if order.price is not None and order.order_type == "limit":
            body["price"] = str(order.price)
        if order.stop_loss is not None:
            body["stopLoss"] = str(order.stop_loss)
        if order.take_profit is not None:
            body["takeProfit"] = str(order.take_profit)
        if order.client_order_id:
            body["orderLinkId"] = order.client_order_id
        if order.reduce_only:
            body["reduceOnly"] = True

        result = await self._signed("POST", "/v5/order/create", body=body)
        return OrderResult(
            exchange_order_id=str(result.get("orderId")),
            status="pending",
            raw=result,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        await self._signed(
            "POST",
            "/v5/order/cancel",
            body={"category": "linear", "symbol": symbol, "orderId": order_id},
        )
        return True

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        params: dict[str, Any] = {"category": "linear", "openOnly": 0}
        if symbol:
            params["symbol"] = symbol
        result = await self._signed("GET", "/v5/order/realtime", params=params)
        out: list[OrderResult] = []
        for o in result.get("list", []):
            status = (o.get("orderStatus") or "").lower()
            mapped = {
                "new": "open",
                "partiallyfilled": "open",
                "filled": "filled",
                "cancelled": "cancelled",
                "rejected": "rejected",
            }.get(status, "pending")
            out.append(
                OrderResult(
                    exchange_order_id=o["orderId"],
                    status=mapped,  # type: ignore[arg-type]
                    avg_fill_price=float(o.get("avgPrice") or 0) or None,
                    filled_qty=float(o.get("cumExecQty") or 0),
                    raw=o,
                )
            )
        return out

    async def close(self) -> None:
        await self._http.aclose()
        if self._direct_http is not None:
            await self._direct_http.aclose()
        await self._pub_http.aclose()
        await self._okx_http.aclose()
