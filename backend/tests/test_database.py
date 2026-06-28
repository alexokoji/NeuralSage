import asyncio
from types import SimpleNamespace

import pytest
from pymongo.errors import ConfigurationError

from app.database import init_db


def test_init_db_handles_mongo_configuration_errors(monkeypatch):
    class DummyClient:
        def __getitem__(self, name):
            return SimpleNamespace(name=name)

        def close(self):
            return None

    async def fake_init_beanie(*args, **kwargs):
        raise ConfigurationError("simulated mongo outage")

    monkeypatch.setattr("app.database.get_motor_client", lambda: DummyClient())
    monkeypatch.setattr("app.database.init_beanie", fake_init_beanie)

    asyncio.run(init_db())
