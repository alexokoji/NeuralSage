import asyncio
from types import SimpleNamespace

from app.services.trading_engine import TradingEngine


class FakePositionQuery:
    def __init__(self, positions):
        self._positions = positions

    def sort(self, *args, **kwargs):
        return self

    async def to_list(self):
        return self._positions


class DummyField:
    def __init__(self, name: str):
        self.name = name

    def __neg__(self):
        return self

    def __eq__(self, other):
        return self


class DummyPosition:
    agent_id = DummyField("agent_id")
    symbol = DummyField("symbol")
    is_open = DummyField("is_open")
    opened_at = DummyField("opened_at")

    @staticmethod
    def find(*args, **kwargs):
        raise RuntimeError("find not patched")


def test_persist_open_trade_creates_trade_and_position(monkeypatch):
    inserted = []

    class DummyTrade:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.id = "trade-1"

        async def insert(self):
            inserted.append(("trade", self.kwargs))

    class DummyPositionDoc:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def insert(self):
            inserted.append(("position", self.kwargs))

    class DummyAgent:
        def __init__(self):
            self.total_trades = 0
            self.session_trade_count = 0
            self.last_trade_at = None
            self.confidence_score = 50.0
            self.recovery_mode = False
            self.strategy = None
            self.name = "TEST"
            self.user_id = "user-1"
            self.id = "agent-1"
            self.saved = False

        async def save(self):
            self.saved = True

    async def fake_notification_create(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.trading_engine.Trade", DummyTrade)
    monkeypatch.setattr("app.services.trading_engine.Position", DummyPositionDoc)
    monkeypatch.setattr("app.services.trading_engine.NotificationService.create", fake_notification_create)

    engine = TradingEngine()
    agent = DummyAgent()
    api_key = SimpleNamespace(id="api-key", exchange="bybit")
    placed = SimpleNamespace(exchange_order_id="ext-123", status="filled", avg_fill_price=100.0, filled_qty=0.01, raw={})
    signal = SimpleNamespace(confidence=0.87, reason="test", metadata={"source": "unit-test"})

    asyncio.run(
        engine._persist_open_trade(
            agent=agent,
            api_key=api_key,
            placed=placed,
            symbol="BTCUSDT",
            side="long",
            entry_price=100.0,
            quantity=0.01,
            stop_loss_pct=1.5,
            take_profit_pct=3.0,
            signal=signal,
            risk_payload={"approved": True, "reason": "test"},
        )
    )

    assert agent.total_trades == 1
    assert agent.session_trade_count == 1
    assert agent.saved is True
    assert any(kind == "trade" for kind, _ in inserted)
    assert any(kind == "position" for kind, _ in inserted)


def test_open_position_returns_latest_open_position(monkeypatch):
    latest = SimpleNamespace(symbol="BTCUSDT", is_open=True, opened_at=2)
    older = SimpleNamespace(symbol="BTCUSDT", is_open=True, opened_at=1)

    def fake_find(*args, **kwargs):
        return FakePositionQuery([latest, older])

    DummyPosition.find = staticmethod(fake_find)
    monkeypatch.setattr("app.services.trading_engine.Position", DummyPosition)

    position = asyncio.run(TradingEngine()._open_position("agent-id", "BTCUSDT"))

    assert position is latest


def test_open_position_returns_none_when_no_open_positions(monkeypatch):
    def fake_find(*args, **kwargs):
        return FakePositionQuery([])

    DummyPosition.find = staticmethod(fake_find)
    monkeypatch.setattr("app.services.trading_engine.Position", DummyPosition)

    position = asyncio.run(TradingEngine()._open_position("agent-id", "BTCUSDT"))

    assert position is None
