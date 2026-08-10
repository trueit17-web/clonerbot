"""Tests for the optimizer sweep (offline, fake candles)."""

from __future__ import annotations

from clonerbot.backtest.engine import BacktestSignal
from clonerbot.backtest.optimize import Combo, Optimizer, default_grid, prefetch


def _candle(o, h, low, c):
    return [0, o, h, low, c, 0]


class _FakeSource:
    def __init__(self, series):
        self._series = series

    async def ohlcv(self, symbol, since_ms, timeframe, limit):
        return self._series.get(symbol, [])


def test_default_grid_nonempty():
    grid = default_grid()
    assert len(grid) > 0 and all(isinstance(c, Combo) for c in grid)


async def test_prefetch_only_keeps_signals_with_data():
    src = _FakeSource({"BTC/USDT": [_candle(100, 101, 99, 100)]})
    signals = [
        BacktestSignal("@a", "BTC/USDT", 0, 100, 95, 110),
        BacktestSignal("@b", "ETH/USDT", 0, 100, 95, 110),  # no data
    ]
    cached = await prefetch(src, signals, "5m", 10)
    assert len(cached) == 1 and cached[0][0].channel == "@a"


async def test_optimizer_prefers_higher_expectancy():
    # A price path that rises to 112: a tight 3% TP wins small; no-TP rides to end.
    candles = [_candle(100, 105, 99, 104), _candle(104, 112, 103, 111)]
    src = _FakeSource({"BTC/USDT": candles})
    cached = await prefetch(src, [BacktestSignal("@x", "BTC/USDT", 0, 100, 0, None)], "5m", 10)

    opt = Optimizer(cached, min_trades=1)
    grid = [
        Combo(stop_pct=0.05, tp_pct=0.03, trailing_pct=0.0, max_hold_bars=0),  # +3%
        Combo(stop_pct=0.05, tp_pct=0.0, trailing_pct=0.0, max_hold_bars=0),   # +11% (end)
    ]
    ranked = opt.run(grid)
    # The no-TP combo captures the full move → higher expectancy → ranked first.
    assert ranked[0].combo.tp_pct == 0.0
    assert ranked[0].avg_return > ranked[1].avg_return


async def test_optimizer_min_trades_filter_falls_back():
    src = _FakeSource({"BTC/USDT": [_candle(100, 110, 99, 108)]})
    cached = await prefetch(src, [BacktestSignal("@x", "BTC/USDT", 0, 100, 95, 108)], "5m", 10)
    # min_trades higher than available → falls back to ranking all combos (no crash)
    ranked = Optimizer(cached, min_trades=100).run(default_grid())
    assert len(ranked) > 0
