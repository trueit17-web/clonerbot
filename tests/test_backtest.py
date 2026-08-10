"""Backtest tests — pure simulation and aggregation, no network."""

from __future__ import annotations

from clonerbot.backtest.engine import (
    Backtester,
    BacktestSignal,
    simulate_trade,
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
