"""Tests for freqtrade-style protections (locks) and their risk integration."""

from __future__ import annotations

from datetime import datetime, timezone

from clonerbot.config import Settings
from clonerbot.execution.executor import Executor
from clonerbot.models.signal import NormalizedSignal, ParseMethod, Side
from clonerbot.risk.protections import (
    GLOBAL_KEY,
    LockStore,
    ProtectionManager,
    channel_key,
)
from clonerbot.risk.risk_engine import PortfolioState, RiskEngine, TradePlan
from clonerbot.scoring.channel_scorer import ChannelScorer


def _settings(**over) -> Settings:
    base = dict(
        _env_file=None, exchanges={}, mode="paper", symbol_whitelist=["BTC", "ETH"],
        cooldown_minutes=5, stoploss_guard_count=3, stoploss_guard_window_min=60,
        stoploss_guard_lock_min=60, losing_streak_count=3, losing_streak_lock_min=120,
    )
    base.update(over)
    return Settings(**base)


def _signal(channel="@vip", base="BTC") -> NormalizedSignal:
    return NormalizedSignal(
        channel=channel, message_id=1, posted_at=datetime.now(timezone.utc),
        parse_method=ParseMethod.regex, base=base, entries=[60000.0],
        take_profits=[66000.0], stop_loss=58800.0,
    )


def _state() -> PortfolioState:
    return PortfolioState(equity=10000.0, peak_equity=10000.0, realized_pnl_today=0.0,
                          open_symbols=set(), open_count=0, tradable=10000.0)


def _plan(symbol="BTC/USDT", sl=58800.0, tp=66000.0) -> TradePlan:
    return TradePlan(True, "ok", symbol=symbol, side=Side.buy, qty=0.01,
                     entry_price=60000.0, stop_loss=sl, take_profit=tp)


# ------------------------------------------------------------------ lock store
async def test_lock_store_add_and_active():
    locks = LockStore()
    assert await locks.active_reason([GLOBAL_KEY]) is None
    await locks.add(GLOBAL_KEY, 10, "test lock")
    assert await locks.active_reason([GLOBAL_KEY]) == "test lock"
    assert await locks.active_reason(["channel:@x"]) is None


async def test_lock_zero_minutes_noop():
    locks = LockStore()
    await locks.add(GLOBAL_KEY, 0, "should not persist")
    assert await locks.active_reason([GLOBAL_KEY]) is None


# ------------------------------------------------------------- risk integration
async def test_risk_rejects_when_locked():
    locks = LockStore()
    eng = RiskEngine(_settings(), ChannelScorer(), locks=locks)
    await locks.add(channel_key("@vip"), 30, "cooldown")
    plan = await eng.evaluate(_signal(), _state(), market_price=60000)
    assert not plan.approved and "locked" in plan.reason


async def test_risk_ok_when_other_channel_locked():
    locks = LockStore()
    eng = RiskEngine(_settings(), ChannelScorer(), locks=locks)
    await locks.add(channel_key("@other"), 30, "cooldown")
    plan = await eng.evaluate(_signal(channel="@vip"), _state(), market_price=60000)
    assert plan.approved


# ------------------------------------------------------------ protection manager
async def test_cooldown_locks_channel_on_close():
    s = _settings()
    locks = LockStore()
    pm = ProtectionManager(s, locks)
    await pm.on_close("@vip", "BTC/USDT", pnl=5.0, reason="tp")
    assert await locks.active_reason([channel_key("@vip")]) is not None


async def test_stoploss_guard_locks_globally(fake_router):
    # Three SL closes should trip the global stoploss guard.
    s = _settings(cooldown_minutes=0)  # isolate the guard from cooldown noise
    locks = LockStore()
    pm = ProtectionManager(s, locks)
    ex = Executor(settings=s, router=fake_router, scorer=ChannelScorer(), protections=pm)
    for i in range(3):
        await ex.open_position(_plan(symbol="BTC/USDT"), channel=f"@c{i}", signal_id=None)
        fake_router.set_price("BTC/USDT", 58000.0)  # below SL
        await ex._check_positions()
    assert await locks.active_reason([GLOBAL_KEY]) is not None


async def test_losing_streak_locks_channel(fake_router):
    s = _settings(cooldown_minutes=0, losing_streak_count=3)
    locks = LockStore()
    pm = ProtectionManager(s, locks)
    ex = Executor(settings=s, router=fake_router, scorer=ChannelScorer(), protections=pm)
    for _ in range(3):
        await ex.open_position(_plan(), channel="@loser", signal_id=None)
        fake_router.set_price("BTC/USDT", 58000.0)
        await ex._check_positions()
        fake_router.set_price("BTC/USDT", 60000.0)  # reset for next open
    assert await locks.active_reason([channel_key("@loser")]) is not None
