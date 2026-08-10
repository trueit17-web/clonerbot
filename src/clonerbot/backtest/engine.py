"""Backtest engine.

Replays each logged BUY signal against historical OHLCV candles and records the
outcome (take-profit / stop-loss / time / end-of-data) and its return. Results
are aggregated per channel — win rate, average return, total return — which is
exactly the signal you need to judge which channels are worth real money.

The price source is injected (`PriceSource` protocol) so the simulation is
fully testable offline; the CLI wires in a CCXT-backed public-data source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

# One OHLCV candle: [timestamp_ms, open, high, low, close, volume]
Candle = list[float]


class PriceSource(Protocol):
    async def ohlcv(self, symbol: str, since_ms: int, timeframe: str, limit: int) -> list[Candle]:
        ...


@dataclass
class BacktestSignal:
    channel: str
    symbol: str
    posted_ms: int
    entry: float
    stop_loss: float
    take_profit: float | None


@dataclass
class TradeResult:
    channel: str
    symbol: str
    reason: str        # tp | sl | time | end | no_data
    entry: float
    exit: float
    ret: float         # fractional return of the spot long


def simulate_trade(
    candles: list[Candle],
    entry: float,
    stop_loss: float,
    take_profit: float | None,
    max_hold_bars: int = 0,
    trailing_pct: float = 0.0,
) -> tuple[str, float]:
    """Walk candles after entry and return (reason, exit_price).

    Conservative when a single candle spans both stop and target: assume the
    stop is hit first. With `trailing_pct` the stop ratchets up to
    high_water*(1-trailing_pct) using highs from PRIOR candles (so it can't
    trail up and stop out within the same candle). `max_hold_bars` (0 =
    unlimited) closes at that bar's close; if candles run out, close at last.
    """
    if not candles:
        return "no_data", entry
    stop = stop_loss
    high_water = entry
    last_close = entry
    for i, c in enumerate(candles):
        _, _o, high, low, close, *_ = c
        last_close = close
        if low <= stop:
            return "sl", stop
        if take_profit is not None and high >= take_profit:
            return "tp", take_profit
        if max_hold_bars and (i + 1) >= max_hold_bars:
            return "time", close
        if trailing_pct > 0:
            high_water = max(high_water, high)
            stop = max(stop, high_water * (1 - trailing_pct))
    return "end", last_close


def _levels_from_row(entries_json, tp_json, stop_loss) -> tuple[list[float], list[float]] | None:
    try:
        entries = json.loads(entries_json) if entries_json else []
        tps = json.loads(tp_json) if tp_json else []
    except (TypeError, ValueError):
        return None
    return entries, tps


@dataclass
class ChannelReport:
    channel: str
    trades: int = 0
    wins: int = 0
    sum_return: float = 0.0
    returns: list[float] = field(default_factory=list)

    @property
    def winrate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_return(self) -> float:
        return self.sum_return / self.trades if self.trades else 0.0


@dataclass
class BacktestReport:
    per_channel: dict[str, ChannelReport] = field(default_factory=dict)
    total_trades: int = 0
    skipped: int = 0

    def record(self, r: TradeResult) -> None:
        cr = self.per_channel.setdefault(r.channel, ChannelReport(r.channel))
        cr.trades += 1
        cr.returns.append(r.ret)
        cr.sum_return += r.ret
        if r.ret > 0:
            cr.wins += 1
        self.total_trades += 1

    def ranked(self) -> list[ChannelReport]:
        return sorted(self.per_channel.values(), key=lambda c: c.sum_return, reverse=True)


class Backtester:
    def __init__(self, price_source: PriceSource, timeframe: str = "5m",
                 bars: int = 288, max_hold_bars: int = 0, default_stop: float = 0.03) -> None:
        self._src = price_source
        self._tf = timeframe
        self._bars = bars
        self._max_hold = max_hold_bars
        self._default_stop = default_stop

    async def run(self, signals: list[BacktestSignal]) -> BacktestReport:
        report = BacktestReport()
        for sig in signals:
            try:
                candles = await self._src.ohlcv(sig.symbol, sig.posted_ms, self._tf, self._bars)
            except Exception:
                report.skipped += 1
                continue
            if not candles:
                report.skipped += 1
                continue
            # Resolve fallbacks: entry = signal entry or first candle open;
            # stop = signal stop or a default fraction below entry.
            entry = sig.entry if sig.entry > 0 else float(candles[0][1])
            if entry <= 0:
                report.skipped += 1
                continue
            stop = sig.stop_loss if sig.stop_loss > 0 else entry * (1 - self._default_stop)
            reason, exit_price = simulate_trade(
                candles, entry, stop, sig.take_profit, self._max_hold
            )
            if reason == "no_data":
                report.skipped += 1
                continue
            ret = (exit_price - entry) / entry
            report.record(TradeResult(sig.channel, sig.symbol, reason, entry, exit_price, ret))
        return report
