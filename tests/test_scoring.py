"""Tests for Edge-style expectancy sizing in the channel scorer."""

from __future__ import annotations

from clonerbot.config import Settings
from clonerbot.execution.executor import Executor
from clonerbot.models.signal import Side
from clonerbot.risk.risk_engine import TradePlan
from clonerbot.scoring.channel_scorer import MIN_MULTIPLIER, ChannelScorer


def _settings(**over) -> Settings:
    base = dict(_env_file=None, exchanges={}, mode="paper", symbol_whitelist=["BTC"],
                use_expectancy_sizing=True, expectancy_min_trades=2, min_expectancy=0.0,
                cooldown_minutes=0)
    base.update(over)
    return Settings(**base)


def _plan() -> TradePlan:
    return TradePlan(True, "ok", symbol="BTC/USDT", side=Side.buy, qty=0.01,
                     entry_price=60000.0, stop_loss=58800.0, take_profit=66000.0)


async def _run_trades(ex, channel, exit_price, n=3):
    for _ in range(n):
        await ex.open_position(_plan(), channel=channel, signal_id=None)
        ex.router.set_price("BTC/USDT", exit_price)
        await ex._check_positions()
        ex.router.set_price("BTC/USDT", 60000.0)


async def test_expectancy_gives_full_size_to_winner(fake_router):
    s = _settings()
    ex = Executor(settings=s, router=fake_router, scorer=ChannelScorer())
    await _run_trades(ex, "@good", exit_price=66000.0)   # ~+10% each
    assert await ChannelScorer(s).multiplier("@good") > MIN_MULTIPLIER


async def test_expectancy_skips_losing_channel(fake_router):
    s = _settings()
    ex = Executor(settings=s, router=fake_router, scorer=ChannelScorer())
    await _run_trades(ex, "@bad", exit_price=58000.0)    # all stop-outs
    assert await ChannelScorer(s).multiplier("@bad") == 0.0


async def test_low_sample_falls_back_to_min(fake_router):
    s = _settings(expectancy_min_trades=10)  # not enough trades → win-rate fallback
    ex = Executor(settings=s, router=fake_router, scorer=ChannelScorer())
    await _run_trades(ex, "@new", exit_price=66000.0, n=2)
    # < _MIN_SAMPLE(5) closed → conservative MIN_MULTIPLIER
    assert await ChannelScorer(s).multiplier("@new") == MIN_MULTIPLIER


async def test_no_settings_disables_expectancy():
    # A bare scorer (no settings) never applies expectancy → MIN for unknown ch.
    assert await ChannelScorer().multiplier("@whatever") == MIN_MULTIPLIER
