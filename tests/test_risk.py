"""Risk engine tests — sizing math and every rejection path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from clonerbot.config import Settings
from clonerbot.models.signal import NormalizedSignal, ParseMethod, Side
from clonerbot.risk.risk_engine import PortfolioState, RiskEngine
from clonerbot.scoring.channel_scorer import ChannelScorer


def _settings(**over) -> Settings:
    base = dict(
        _env_file=None,
        exchanges={},
        symbol_whitelist=["BTC", "ETH", "SOL"],
        risk_per_trade=0.01,
        max_position_fraction=0.5,  # loosen cap so risk-based sizing is visible
        max_open_positions=3,
        daily_loss_limit=0.05,
        max_drawdown=0.2,
    )
    base.update(over)
    return Settings(**base)


def _signal(**over) -> NormalizedSignal:
    data = dict(
        channel="@vip",
        message_id=1,
        posted_at=datetime.now(timezone.utc),
        parse_method=ParseMethod.regex,
        base="BTC",
        entries=[60000.0],
        take_profits=[66000.0],
        stop_loss=58800.0,
    )
    data.update(over)
    return NormalizedSignal(**data)


def _state(**over) -> PortfolioState:
    data = dict(
        equity=10000.0, peak_equity=10000.0, realized_pnl_today=0.0,
        open_symbols=set(), open_count=0, killed=False,
    )
    data.update(over)
    return PortfolioState(**data)


@pytest.fixture
def engine():
    return RiskEngine(_settings(), ChannelScorer())


async def test_risk_based_sizing_loss_equals_risk_budget(engine):
    # New channel → 0.5x multiplier. risk budget = 10000 * 0.01 * 0.5 = 50.
    plan = await engine.evaluate(_signal(), _state(), market_price=60000)
    assert plan.approved
    loss_if_stop = plan.qty * (plan.entry_price - plan.stop_loss)
    assert loss_if_stop == pytest.approx(50.0, rel=1e-3)


async def test_position_fraction_cap(engine):
    # Very tight stop would size huge; cap must bind at max_position_fraction.
    s = _settings(max_position_fraction=0.1)
    eng = RiskEngine(s, ChannelScorer())
    plan = await eng.evaluate(_signal(stop_loss=59000.0), _state(), market_price=60000)
    assert plan.approved
    notional = plan.qty * plan.entry_price
    assert notional <= 10000 * 0.1 + 1e-6


async def test_reject_not_whitelisted(engine):
    plan = await engine.evaluate(_signal(base="PEPE"), _state(), 1.0)
    assert not plan.approved and "whitelist" in plan.reason


async def test_reject_sell_side_spot(engine):
    plan = await engine.evaluate(_signal(side=Side.sell), _state(), 60000)
    assert not plan.approved


async def test_reject_stale(engine):
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    plan = await engine.evaluate(_signal(posted_at=old), _state(), 60000)
    assert not plan.approved and "stale" in plan.reason


async def test_reject_duplicate(engine):
    plan = await engine.evaluate(_signal(), _state(open_symbols={"BTC/USDT"}, open_count=1), 60000)
    assert not plan.approved


async def test_reject_max_open(engine):
    st = _state(open_symbols={"A/USDT", "B/USDT", "C/USDT"}, open_count=3)
    plan = await engine.evaluate(_signal(), st, 60000)
    assert not plan.approved and "max open" in plan.reason


async def test_reject_daily_loss(engine):
    st = _state(equity=9400, realized_pnl_today=-600)
    plan = await engine.evaluate(_signal(), st, 60000)
    assert not plan.approved and "daily loss" in plan.reason


async def test_reject_drawdown(engine):
    st = _state(equity=7900, peak_equity=10000)
    plan = await engine.evaluate(_signal(), st, 60000)
    assert not plan.approved and "drawdown" in plan.reason


async def test_reject_killed(engine):
    plan = await engine.evaluate(_signal(), _state(killed=True), 60000)
    assert not plan.approved and "KILL" in plan.reason


async def test_invalid_stop_above_entry(engine):
    plan = await engine.evaluate(_signal(stop_loss=61000.0), _state(), 60000)
    assert not plan.approved


async def test_default_stop_applied_when_missing(engine):
    plan = await engine.evaluate(_signal(stop_loss=None), _state(), 60000)
    assert plan.approved
    # default_stop_loss = 0.03 → stop at 60000 * 0.97
    assert plan.stop_loss == pytest.approx(60000 * 0.97)
