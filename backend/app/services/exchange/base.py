"""Common interface and value-objects for exchange clients.

Every exchange adapter MUST:
  * Refuse withdrawal-capable keys (we verify permissions on first contact).
  * Sign every authenticated request server-side; secrets never leave the host.
  * Bubble up rate-limit / signature failures as ExchangeError.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


class ExchangeError(Exception):
    """Generic exchange-side failure (network, signature, rate-limit, business)."""


class InsufficientPermissions(ExchangeError):
    """Raised when a key is missing read/trade or carries withdrawal."""


@dataclass
class OrderRequest:
    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    quantity: float
    price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    client_order_id: str | None = None
    reduce_only: bool = False


@dataclass
class OrderResult:
    exchange_order_id: str
    status: Literal["pending", "open", "filled", "cancelled", "rejected"]
    avg_fill_price: float | None = None
    filled_qty: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Balance:
    asset: str
    available: float
    total: float
    usd_value: float | None = None


@dataclass
class Ticker:
    symbol: str
    last: float
    bid: float
    ask: float
    high_24h: float
    low_24h: float
    volume_24h: float
    change_24h_pct: float


@dataclass
class Candle:
    open_time: int  # ms epoch
    open: float
    high: float
    low: float
    close: float
    volume: float


class ExchangeClient(ABC):
    """Abstract authenticated exchange client."""

    name: str
    is_testnet: bool

    @abstractmethod
    async def verify_permissions(self) -> list[str]:
        """Return the verified permission set. Raise if `withdraw` is granted."""

    @abstractmethod
    async def get_balances(self) -> list[Balance]: ...

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker: ...

    @abstractmethod
    async def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]: ...

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[OrderResult]: ...

    @abstractmethod
    async def close(self) -> None: ...
