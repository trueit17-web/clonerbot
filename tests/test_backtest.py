"""Backtest tests — pure simulation and aggregation, no network."""

from __future__ import annotations

import pytest

from clonerbot.backtest.engine import (
    Backtester,
    BacktestSignal,
    simulate_trade,
    trade_return,
)


def _candle(o, h, low, c):
    return [0, o, h, low, c, 0]


# --------------------------------------------------------------- simulate_trade
def test_take_profit_hit():
    candles = [_candle(100, 101, 99, 100), _candle(100, 110, 99, 105)]
    reason, price = simulate_trade(candles, entry=100, stop_loss=95, take_profit=108)
    assert reason == "tp" and price == 108


def test_stop_loss_hit():
    candles = [_candle(100, 101, 99, 100), _candle(100, 101, 90, 92)]
    reason, price = simulate_trade(candles, entry=100, stop_loss=95, take_profit=120)
    assert reason == "sl" and price == 95


def test_stop_before_target_same_candle():
    # A candle that spans both → conservative: stop first.
    candles = [_candle(100, 130, 90, 100)]
    reason, price = simulate_trade(candles, entry=100, stop_loss=95, take_profit=120)
    assert reason == "sl"


def test_max_hold_time_exit():
    candles = [_candle(100, 101, 99, 100), _candle(100, 101, 99, 102), _candle(100, 101, 99, 103)]
    reason, price = simulate_trade(candles, entry=100, stop_loss=90, take_profit=None,
                                   max_hold_bars=2)
    assert reason == "time" and price == 102


def test_end_of_data():
    candles = [_candle(100, 101, 99, 100), _candle(100, 104, 99, 103)]
    reason, price = simulate_trade(candles, entry=100, stop_loss=90, take_profit=200)
    assert reason == "end" and price == 103


def test_no_data():
    reason, price = simulate_trade([], entry=100, stop_loss=90, take_profit=110)
    assert reason == "no_data"


def test_trailing_stop_locks_in_gain():
    # Rise to 120 (trail stop to 108), then drop to 105 → exit at trailed stop.
    candles = [_candle(100, 120, 100, 118), _candle(118, 118, 105, 106)]
    reason, price = simulate_trade(candles, entry=100, stop_loss=90, take_profit=None,
                                   trailing_pct=0.10)
    assert reason == "sl" and price == 108  # 120*(1-0.10), a profitable exit


def test_trailing_does_not_stop_same_candle_as_new_high():
    # Single candle with a high but low above the trailed stop → survives.
    candles = [_candle(100, 120, 109, 115)]
    reason, price = simulate_trade(candles, entry=100, stop_loss=90, take_profit=None,
                                   trailing_pct=0.10)
    assert reason == "end"  # low 109 > trailed stop 108, computed only for next bar


# ------------------------------------------------------------------ Backtester
class _FakeSource:
    def __init__(self, series: dict[str, list]):
        self._series = series

    async def ohlcv(self, symbol, since_ms, timeframe, limit):
        return self._series.get(symbol, [])


async def test_backtester_aggregates_per_channel():
    src = _FakeSource({
        "BTC/USDT": [_candle(100, 101, 99, 100), _candle(100, 110, 99, 108)],  # TP
        "ETH/USDT": [_candle(100, 101, 90, 92)],                               # SL
    })
    signals = [
        BacktestSignal("@good", "BTC/USDT", 0, entry=100, stop_loss=95, take_profit=108),
        BacktestSignal("@bad", "ETH/USDT", 0, entry=100, stop_loss=95, take_profit=120),
    ]
    report = await Backtester(src).run(signals)
    assert report.total_trades == 2
    good = report.per_channel["@good"]
    bad = report.per_channel["@bad"]
    assert good.wins == 1 and good.avg_return > 0
    assert bad.wins == 0 and bad.avg_return < 0
    # ranking puts the profitable channel first
    assert report.ranked()[0].channel == "@good"


async def test_backtester_entry_fallback_to_first_open():
    # entry=0 → use first candle open (100); default_stop 3% → stop 97; TP 108 hit
    src = _FakeSource({"BTC/USDT": [_candle(100, 101, 99, 100), _candle(100, 110, 99, 108)]})
    sig = BacktestSignal("@x", "BTC/USDT", 0, entry=0.0, stop_loss=0.0, take_profit=108)
    report = await Backtester(src, default_stop=0.03).run([sig])
    tr = report.per_channel["@x"]
    assert tr.trades == 1 and tr.wins == 1


# -------------------------------------------------------------- shorts + leverage
def test_short_take_profit_and_stop():
    # short: tp below, stop above.
    down = [_candle(100, 101, 88, 90)]
    reason, px = simulate_trade(down, entry=100, stop_loss=105, take_profit=90, side="sell")
    assert reason == "tp" and px == 90
    up = [_candle(100, 108, 99, 106)]
    reason, px = simulate_trade(up, entry=100, stop_loss=105, take_profit=90, side="sell")
    assert reason == "sl" and px == 105


def test_trade_return_sign_by_side():
    assert trade_return("buy", 100, 110) == pytest.approx(0.10)
    assert trade_return("sell", 100, 90) == pytest.approx(0.10)   # short gains on drop
    assert trade_return("sell", 100, 110) == pytest.approx(-0.10)


def test_leverage_multiplies_return():
    assert trade_return("buy", 100, 110, leverage=5) == pytest.approx(0.50)


def test_liquidation_on_long_with_leverage():
    # 10x long: liquidation ≈ entry*0.9 = 90. A drop to 89 liquidates.
    candles = [_candle(100, 101, 89, 95)]
    reason, px = simulate_trade(candles, entry=100, stop_loss=80, take_profit=120,
                                side="buy", leverage=10)
    assert reason == "liq" and px == pytest.approx(90.0)
    # leveraged return at liquidation ≈ -100% of margin
    assert trade_return("buy", 100, px, leverage=10) == pytest.approx(-1.0)


async def test_backtester_short_profits_on_drop():
    src = _FakeSource({"ETH/USDT": [_candle(100, 101, 88, 90)]})
    sig = BacktestSignal("@s", "ETH/USDT", 0, entry=100, stop_loss=105,
                         take_profit=90, side="sell")
    report = await Backtester(src).run([sig])
    assert report.per_channel["@s"].wins == 1
    assert report.per_channel["@s"].avg_return > 0


async def test_backtester_skips_missing_history():
    src = _FakeSource({})  # no data for any symbol
    sig = BacktestSignal("@x", "BTC/USDT", 0, entry=100, stop_loss=95, take_profit=110)
    report = await Backtester(src).run([sig])
    assert report.total_trades == 0 and report.skipped == 1


# ---------------------------------------------------------------------- loader
async def test_loader_reads_stored_levels_and_reparses():
    import json
    from datetime import datetime, timezone

    from clonerbot.backtest.loader import load_signals
    from clonerbot.db import session_scope
    from clonerbot.models.db import SignalRecord

    async with session_scope() as s:
        # row A: structured levels stored
        s.add(SignalRecord(
            channel="@a", message_id=1, raw_text="x", posted_at=datetime.now(timezone.utc),
            symbol="BTC/USDT", side="buy", entries=json.dumps([60000.0]),
            take_profits=json.dumps([66000.0]), stop_loss=58800.0, status="executed",
        ))
        # row B: no structured levels → re-parse raw_text
        s.add(SignalRecord(
            channel="@b", message_id=2,
            raw_text="ETH/USDT buy entry 3000 tp 3300 sl 2900",
            posted_at=datetime.now(timezone.utc), status="parsed",
        ))
        # row C: not a buy signal → skipped
        s.add(SignalRecord(
            channel="@c", message_id=3, raw_text="gm bullish",
            posted_at=datetime.now(timezone.utc), status="quarantined",
        ))

    sigs = {s.channel: s for s in await load_signals()}
    assert set(sigs) == {"@a", "@b"}
    assert sigs["@a"].symbol == "BTC/USDT" and sigs["@a"].take_profit == 66000.0
    assert sigs["@b"].symbol == "ETH/USDT" and sigs["@b"].stop_loss == 2900.0
