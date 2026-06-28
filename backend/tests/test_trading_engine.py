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
