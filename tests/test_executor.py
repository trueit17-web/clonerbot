"""Executor + pipeline tests in paper mode, fully offline via FakeRouter."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from clonerbot.config import Settings
from clonerbot.execution.executor import Executor
from clonerbot.models.signal import RawMessage, Side
from clonerbot.risk.risk_engine import RiskEngine, TradePlan
from clonerbot.scoring.channel_scorer import ChannelScorer


def _settings(**over) -> Settings:
    base = dict(_env_file=None, exchanges={}, mode="paper", paper_start_equity=10000.0,
                symbol_whitelist=["BTC", "ETH", "SOL"])
    base.update(over)
    return Settings(**base)


@pytest.fixture
def executor(fake_router):
    return Executor(settings=_settings(), router=fake_router, scorer=ChannelScorer())


def _plan(symbol="BTC/USDT", qty=0.01, entry=60000.0, sl=58800.0, tp=66000.0) -> TradePlan:
    return TradePlan(True, "ok", symbol=symbol, side=Side.buy, qty=qty,
                     entry_price=entry, stop_loss=sl, take_profit=tp)


async def test_open_reduces_cash_and_tracks_position(executor):
    pos = await executor.open_position(_plan(), channel="@vip", signal_id=None)
    assert pos is not None
    assert "BTC/USDT" in executor.open_positions
    # cash spent ~ qty*price + fee
    assert executor.paper.cash < 10000.0


async def test_take_profit_closes_with_profit(executor, fake_router):
    await executor.open_position(_plan(), channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 66000.0)  # TP hit
    await executor._check_positions()
    assert "BTC/USDT" not in executor.open_positions
    # realized PnL should be positive
    assert executor._realized_today > 0


async def test_stop_loss_closes_with_loss(executor, fake_router):
    await executor.open_position(_plan(), channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 58000.0)  # below SL
    await executor._check_positions()
    assert "BTC/USDT" not in executor.open_positions
    assert executor._realized_today < 0


async def test_no_duplicate_position(executor):
    await executor.open_position(_plan(), channel="@vip", signal_id=None)
    dup = await executor.open_position(_plan(), channel="@vip", signal_id=None)
    assert dup is None
    assert len(executor.open_positions) == 1


async def test_kill_closes_all(executor):
    await executor.open_position(_plan(), channel="@vip", signal_id=None)
    await executor.open_position(_plan(symbol="ETH/USDT", entry=3000, sl=2900, tp=3300),
                                 channel="@vip", signal_id=None)
    executor.killed = True
    n = await executor.close_all("kill")
    assert n == 2 and not executor.open_positions


async def test_recover_open_positions(fake_router):
    ex1 = Executor(settings=_settings(), router=fake_router, scorer=ChannelScorer())
    await ex1.open_position(_plan(), channel="@vip", signal_id=None)
    # New executor instance (simulating restart) recovers from DB.
    ex2 = Executor(settings=_settings(), router=fake_router, scorer=ChannelScorer())
    await ex2.recover_open_positions()
    assert "BTC/USDT" in ex2.open_positions


async def test_pipeline_end_to_end(fake_router):
    from clonerbot.core.pipeline import Pipeline
    from clonerbot.parser.signal_parser import SignalParser

    settings = _settings()
    scorer = ChannelScorer()
    executor = Executor(settings=settings, router=fake_router, scorer=scorer)
    risk = RiskEngine(settings, scorer)
    parser = SignalParser(use_llm=False)
    pipe = Pipeline(settings, parser, risk, executor, scorer)

    msg = RawMessage(
        channel="@vip", message_id=42,
        text="BTC/USDT buy entry 60000 tp 66000 sl 58800",
        posted_at=datetime.now(timezone.utc),
    )
    await pipe.handle(msg)
    assert "BTC/USDT" in executor.open_positions

    # A quarantined (non-signal) message must NOT open anything.
    msg2 = RawMessage(channel="@vip", message_id=43, text="gm friends bullish vibes",
                      posted_at=datetime.now(timezone.utc))
    await pipe.handle(msg2)
    assert len(executor.open_positions) == 1
