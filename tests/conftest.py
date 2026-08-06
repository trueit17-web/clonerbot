"""Shared test fixtures. Runs everything offline: temp SQLite DB, fake router."""

from __future__ import annotations

import os
import tempfile

import pytest

# Point config at an isolated temp DB BEFORE clonerbot is imported anywhere.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["CLONERBOT_DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp.name}"
os.environ["CLONERBOT_EXCHANGES"] = "{}"
os.environ["CLONERBOT_MODE"] = "paper"
os.environ.setdefault("CLONERBOT_ANTHROPIC_API_KEY", "")


@pytest.fixture(autouse=True)
async def _fresh_db():
    """Create a clean schema for each test."""
    import clonerbot.db as db
    from clonerbot.models.db import Base

    db._engine = None
    db._sessionmaker = None
    engine = db.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


class FakeRouter:
    """Router stand-in with settable prices; never hits a network."""

    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices
        self.clients = {}

    @property
    def has_exchanges(self) -> bool:
        return False

    async def load(self) -> None:
        return None

    async def price(self, symbol: str) -> float | None:
        return self.prices.get(symbol)

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    async def total_quote_equity(self, quote: str = "USDT") -> float:
        return 0.0

    async def pick(self, symbol: str, quote: str):
        return None

    async def close(self) -> None:
        return None


@pytest.fixture
def fake_router():
    return FakeRouter({"BTC/USDT": 60000.0, "ETH/USDT": 3000.0})
