"""Hyperopt-lite: grid-search fixed risk parameters over the backtest.

Candles are fetched ONCE per signal, then every parameter combination is scored
against the cached candles (CPU only), so a full sweep is cheap. Each combo
applies a uniform stop %, take-profit %, trailing % and max-hold to all signals
(overriding their own levels) — the point is to discover robust fixed levels you
can then set via `stop_loss_override_pct` / `take_profit_override_pct` /
`trailing_stop_pct` / `max_hold_minutes`.

Ranked by expectancy (mean return per trade) among combos with enough trades.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from clonerbot.backtest.engine import BacktestSignal, Candle, simulate_trade, trade_return
from clonerbot.logging_conf import get_logger

log = get_logger("optimize")

# Sensible default sweep. tp/trailing 0 mean "disabled" (ride to end / no TP).
DEFAULT_STOPS = [0.02, 0.03, 0.05]
DEFAULT_TPS = [0.0, 0.03, 0.05, 0.08, 0.10]
DEFAULT_TRAILING = [0.0, 0.02, 0.05]
DEFAULT_MAXHOLD = [0, 48, 96]


@dataclass(frozen=True)
class Combo:
    stop_pct: float
    tp_pct: float
    trailing_pct: float
    max_hold_bars: int


@dataclass
class ComboResult:
    combo: Combo
    trades: int
    wins: int
    sum_return: float

    @property
    def winrate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_return(self) -> float:
        return self.sum_return / self.trades if self.trades else 0.0


Cached = list[tuple[BacktestSignal, list[Candle]]]


async def prefetch(source, signals: list[BacktestSignal], timeframe: str, bars: int) -> Cached:
    """Fetch candles once per signal (the only network cost)."""
    cached: Cached = []
    for sig in signals:
        try:
            candles = await source.ohlcv(sig.symbol, sig.posted_ms, timeframe, bars)
        except Exception:
            continue
        if candles:
            cached.append((sig, candles))
    return cached


class Optimizer:
    def __init__(self, cached: Cached, min_trades: int = 10) -> None:
        self._cached = cached
        self._min_trades = min_trades

    def evaluate(self, combo: Combo) -> ComboResult:
        wins = 0
        n = 0
        total = 0.0
        for sig, candles in self._cached:
            entry = sig.entry if sig.entry > 0 else float(candles[0][1])
            if entry <= 0:
                continue
            long = sig.side == "buy"
            # Apply the combo's fixed levels, mirrored for shorts.
            stop = entry * (1 - combo.stop_pct) if long else entry * (1 + combo.stop_pct)
            tp = None
            if combo.tp_pct > 0:
                tp = entry * (1 + combo.tp_pct) if long else entry * (1 - combo.tp_pct)
            reason, px = simulate_trade(
                candles, entry, stop, tp, combo.max_hold_bars, combo.trailing_pct,
                side=sig.side, leverage=sig.leverage,
            )
            if reason == "no_data":
                continue
            ret = trade_return(sig.side, entry, px, sig.leverage)
            total += ret
            n += 1
            if ret > 0:
                wins += 1
        return ComboResult(combo, n, wins, total)

    def run(self, grid: list[Combo]) -> list[ComboResult]:
        results = [self.evaluate(c) for c in grid]
        # Rank by expectancy, but only among combos with a meaningful sample.
        qualified = [r for r in results if r.trades >= self._min_trades]
        pool = qualified or results
        return sorted(pool, key=lambda r: (r.avg_return, r.sum_return), reverse=True)


def default_grid() -> list[Combo]:
    return [
        Combo(s, t, tr, mh)
        for s, t, tr, mh in itertools.product(
            DEFAULT_STOPS, DEFAULT_TPS, DEFAULT_TRAILING, DEFAULT_MAXHOLD
        )
    ]
