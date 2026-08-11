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
    side: str = "buy"        # "buy" = long, "sell" = short
    leverage: float = 1.0


@dataclass
class TradeResult:
    channel: str
    symbol: str
    reason: str        # tp | sl | time | end | no_data
    entry: float
    exit: float
    ret: float         # fractional return of the spot long


def trade_return(side: str, entry: float, exit_price: float, leverage: float = 1.0) -> float:
    """Margin return of a trade: price move in the position's favor × leverage."""
    if entry <= 0:
        return 0.0
    r = (exit_price - entry) / entry if side == "buy" else (entry - exit_price) / entry
    return r * leverage


def simulate_trade(
    candles: list[Candle],
    entry: float,
    stop_loss: float,
    take_profit: float | None,
    max_hold_bars: int = 0,
    trailing_pct: float = 0.0,
    side: str = "buy",
    leverage: float = 1.0,
) -> tuple[str, float]:
    """Walk candles after entry and return (reason, exit_price).

    Direction-aware (long/short) with leverage-based liquidation. Conservative
    when a single candle spans both stop and target: the stop is assumed first.
    Trailing ratchets the stop toward price using PRIOR-candle extremes (so it
    can't trail and stop out within the same candle). reason ∈
    {sl, tp, liq, time, end, no_data}.
    """
    if not candles:
        return "no_data", entry
    long = side == "buy"
    stop = stop_loss
    # Liquidation price (only meaningful with leverage > 1).
    liq = None
    if leverage and leverage > 1:
        liq = entry * (1 - 1 / leverage) if long else entry * (1 + 1 / leverage)
    water = entry  # high-water (long) / low-water (short) for trailing
    last_close = entry
    for i, c in enumerate(candles):
        _, _o, high, low, close, *_ = c
        last_close = close
        if long:
            # Barrier hit first as price falls = the one closest to entry (higher).
            barrier, reason = stop, "sl"
            if liq is not None and liq > barrier:
                barrier, reason = liq, "liq"
            if low <= barrier:
                return reason, barrier
            if take_profit is not None and high >= take_profit:
                return "tp", take_profit
        else:
            barrier, reason = stop, "sl"
            if liq is not None and liq < barrier:
                barrier, reason = liq, "liq"
            if high >= barrier:
                return reason, barrier
            if take_profit is not None and low <= take_profit:
                return "tp", take_profit
        if max_hold_bars and (i + 1) >= max_hold_bars:
            return "time", close
        if trailing_pct > 0:
            if long:
                water = max(water, high)
                stop = max(stop, water * (1 - trailing_pct))
            else:
                water = min(water, low)
                stop = min(stop, water * (1 + trailing_pct))
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
            # stop = signal stop or a default fraction (mirrored for shorts).
            entry = sig.entry if sig.entry > 0 else float(candles[0][1])
            if entry <= 0:
                report.skipped += 1
                continue
            if sig.stop_loss > 0:
                stop = sig.stop_loss
            else:
                stop = (entry * (1 - self._default_stop) if sig.side == "buy"
                        else entry * (1 + self._default_stop))
            reason, exit_price = simulate_trade(
                candles, entry, stop, sig.take_profit, self._max_hold,
                side=sig.side, leverage=sig.leverage,
            )
            if reason == "no_data":
                report.skipped += 1
                continue
            ret = trade_return(sig.side, entry, exit_price, sig.leverage)
            report.record(TradeResult(sig.channel, sig.symbol, reason, entry, exit_price, ret))
        return report
