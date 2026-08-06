"""Tests for the paper slippage model and the trailing stop."""

from __future__ import annotations

import pytest

from clonerbot.config import Settings
from clonerbot.execution.executor import Executor
from clonerbot.execution.paper_broker import TAKER_FEE, PaperBroker
from clonerbot.models.signal import Side
from clonerbot.risk.risk_engine import TradePlan
from clonerbot.scoring.channel_scorer import ChannelScorer


def _settings(**over) -> Settings:
    base = dict(_env_file=None, exchanges={}, mode="paper", paper_start_equity=10000.0,
                symbol_whitelist=["BTC"])
    base.update(over)
    return Settings(**base)


def _plan(symbol="BTC/USDT", qty=0.01, entry=60000.0, sl=58800.0, tp=66000.0) -> TradePlan:
    return TradePlan(True, "ok", symbol=symbol, side=Side.buy, qty=qty,
                     entry_price=entry, stop_loss=sl, take_profit=tp)


# --------------------------------------------------------------------- slippage
def test_slippage_makes_buy_more_expensive_and_sell_cheaper():
    broker = PaperBroker(10_000.0, slippage=0.001)
    fill_buy, cost = broker.buy(1.0, 100.0)
    assert fill_buy == pytest.approx(100.1)          # bought higher
    assert cost == pytest.approx(100.1 * (1 + TAKER_FEE))
    fill_sell, proceeds = broker.sell(1.0, 100.0)
    assert fill_sell == pytest.approx(99.9)          # sold lower
    assert proceeds == pytest.approx(99.9 * (1 - TAKER_FEE))


def test_zero_slippage_is_only_fees():
    broker = PaperBroker(10_000.0, slippage=0.0)
    fill, cost = broker.buy(2.0, 50.0)
    assert fill == 50.0
    assert cost == pytest.approx(100.0 * (1 + TAKER_FEE))


async def test_executor_records_slipped_entry(fake_router):
    ex = Executor(settings=_settings(paper_slippage=0.001), router=fake_router,
                  scorer=ChannelScorer())
    pos = await ex.open_position(_plan(), channel="@vip", signal_id=None)
    # mark 60000, +0.1% slippage → entry recorded above the mark
    assert pos.entry_price == pytest.approx(60000 * 1.001)


# ---------------------------------------------------------------- trailing stop
async def test_trailing_ratchets_stop_up(fake_router):
    ex = Executor(settings=_settings(trailing_stop_pct=0.02, paper_slippage=0.0),
                  router=fake_router, scorer=ChannelScorer())
    pos = await ex.open_position(_plan(sl=58800.0), channel="@vip", signal_id=None)
    assert pos.stop_loss == 58800.0

    # Price climbs to 65000 → trailing stop should rise to 65000*0.98 = 63700.
    fake_router.set_price("BTC/USDT", 65000.0)
    await ex._check_positions()
    assert "BTC/USDT" in ex.open_positions  # not stopped yet
    assert ex.open_positions["BTC/USDT"].stop_loss == pytest.approx(63700.0)


async def test_trailing_never_loosens(fake_router):
    ex = Executor(settings=_settings(trailing_stop_pct=0.02, paper_slippage=0.0),
                  router=fake_router, scorer=ChannelScorer())
    await ex.open_position(_plan(), channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 65000.0)
    await ex._check_positions()
    raised = ex.open_positions["BTC/USDT"].stop_loss
    # Price falls back but not to the stop; stop must NOT move down.
    fake_router.set_price("BTC/USDT", 64000.0)
    await ex._check_positions()
    assert ex.open_positions["BTC/USDT"].stop_loss == pytest.approx(raised)


async def test_trailing_stop_triggers_exit(fake_router):
    ex = Executor(settings=_settings(trailing_stop_pct=0.02, paper_slippage=0.0),
                  router=fake_router, scorer=ChannelScorer())
    await ex.open_position(_plan(tp=None), channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 65000.0)   # stop trails to 63700
    await ex._check_positions()
    fake_router.set_price("BTC/USDT", 63000.0)   # falls below trailed stop
    await ex._check_positions()
    assert "BTC/USDT" not in ex.open_positions   # closed by trailing stop
    assert ex._realized_today > 0                # locked in a gain vs 60000 entry


async def test_trailing_disabled_by_default(fake_router):
    ex = Executor(settings=_settings(paper_slippage=0.0), router=fake_router,
                  scorer=ChannelScorer())
    await ex.open_position(_plan(sl=58800.0), channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 65000.0)
    await ex._check_positions()
    # trailing_stop_pct default 0 → stop unchanged
    assert ex.open_positions["BTC/USDT"].stop_loss == 58800.0
