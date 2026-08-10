"""Futures tests: long/short risk sizing, leverage, liquidation cap, PnL signs."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from clonerbot.config import Settings
from clonerbot.execution.executor import Executor
from clonerbot.models.signal import NormalizedSignal, ParseMethod, Side
from clonerbot.risk.risk_engine import PortfolioState, RiskEngine, TradePlan
from clonerbot.scoring.channel_scorer import ChannelScorer


def _settings(**over) -> Settings:
    base = dict(_env_file=None, exchanges={}, mode="paper", market="futures",
                paper_start_equity=10000.0, symbol_whitelist=["BTC", "ETH"],
                risk_per_trade=0.01, max_position_fraction=0.5, default_leverage=5,
                max_leverage=20, paper_slippage=0.0)
    base.update(over)
    return Settings(**base)


def _signal(side=Side.buy, base="BTC", entries=None, tp=None, sl=None, leverage=None):
    return NormalizedSignal(
        channel="@vip", message_id=1, posted_at=datetime.now(timezone.utc),
        parse_method=ParseMethod.regex, base=base, side=side,
        entries=entries or [60000.0], take_profits=tp or [], stop_loss=sl, leverage=leverage,
    )


def _state(**over) -> PortfolioState:
    d = dict(equity=10000.0, peak_equity=10000.0, realized_pnl_today=0.0,
             open_symbols=set(), open_count=0, tradable=10000.0)
    d.update(over)
    return PortfolioState(**d)


# --------------------------------------------------------------- risk: direction
async def test_futures_allows_short():
    eng = RiskEngine(_settings(), ChannelScorer())
    plan = await eng.evaluate(_signal(side=Side.sell, sl=61200.0, tp=[57000.0]),
                              _state(), market_price=60000)
    assert plan.approved and plan.side is Side.sell
    assert plan.stop_loss > plan.entry_price  # short stop is ABOVE entry
    assert plan.take_profit < plan.entry_price


async def test_short_stop_below_entry_rejected():
    eng = RiskEngine(_settings(), ChannelScorer())
    plan = await eng.evaluate(_signal(side=Side.sell, sl=59000.0), _state(), 60000)
    assert not plan.approved and "short stop" in plan.reason


async def test_long_risk_is_leverage_independent(fake_router):
    # Loss if stop hit ≈ risk_per_trade * equity * mult regardless of leverage.
    eng = RiskEngine(_settings(default_leverage=10), ChannelScorer())
    plan = await eng.evaluate(_signal(sl=58800.0), _state(), 60000)  # 2% stop
    assert plan.approved and plan.leverage >= 1
    loss_if_stop = plan.qty * (plan.entry_price - plan.stop_loss)
    # new channel mult 0.5 → risk budget = 10000*0.01*0.5 = 50
    assert loss_if_stop == pytest.approx(50.0, rel=1e-3)


async def test_leverage_capped_by_liquidation_safety():
    # 10% stop with safety 0.8 → max safe leverage int(0.8/0.10)=8, below requested 20.
    eng = RiskEngine(_settings(default_leverage=20, liquidation_safety=0.8), ChannelScorer())
    plan = await eng.evaluate(_signal(sl=54000.0), _state(), 60000)  # 10% stop
    assert plan.approved and plan.leverage == 8


async def test_leverage_from_signal_used():
    eng = RiskEngine(_settings(default_leverage=3, max_leverage=20), ChannelScorer())
    plan = await eng.evaluate(_signal(sl=59400.0, leverage=10), _state(), 60000)  # 1% stop
    assert plan.approved and plan.leverage == 10


# --------------------------------------------------------------- executor: PnL
def _plan(side=Side.buy, sl=58800.0, tp=66000.0, lev=5.0) -> TradePlan:
    return TradePlan(True, "ok", symbol="BTC/USDT", side=side, qty=0.01,
                     entry_price=60000.0, stop_loss=sl, take_profit=tp,
                     leverage=lev)


async def test_long_profits_when_price_rises(fake_router):
    ex = Executor(settings=_settings(), router=fake_router, scorer=ChannelScorer())
    await ex.open_position(_plan(side=Side.buy), channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 66000.0)  # up → TP for long
    await ex._check_positions()
    assert "BTC/USDT" not in ex.open_positions
    assert ex._realized_today > 0


async def test_short_profits_when_price_falls(fake_router):
    ex = Executor(settings=_settings(), router=fake_router, scorer=ChannelScorer())
    # short: stop above (61200), tp below (57000)
    await ex.open_position(_plan(side=Side.sell, sl=61200.0, tp=57000.0),
                           channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 57000.0)  # down → TP for short
    await ex._check_positions()
    assert "BTC/USDT" not in ex.open_positions
    assert ex._realized_today > 0  # short made money on the drop


async def test_short_stops_out_when_price_rises(fake_router):
    ex = Executor(settings=_settings(), router=fake_router, scorer=ChannelScorer())
    await ex.open_position(_plan(side=Side.sell, sl=61200.0, tp=57000.0),
                           channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 61500.0)  # up → stop for short
    await ex._check_positions()
    assert "BTC/USDT" not in ex.open_positions
    assert ex._realized_today < 0


async def test_leverage_reduces_margin(fake_router):
    # Same qty, higher leverage → less cash locked as margin.
    ex1 = Executor(settings=_settings(), router=fake_router, scorer=ChannelScorer())
    await ex1.open_position(_plan(lev=2.0), channel="@vip", signal_id=None)
    used_2x = 10000.0 - ex1.paper.cash

    fake_router.set_price("BTC/USDT", 60000.0)
    ex2 = Executor(settings=_settings(), router=fake_router, scorer=ChannelScorer())
    await ex2.open_position(_plan(lev=10.0), channel="@vip", signal_id=None)
    used_10x = 10000.0 - ex2.paper.cash
    assert used_10x < used_2x  # 10x locks ~1/5 the margin of 2x
