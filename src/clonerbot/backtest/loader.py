"""Build BacktestSignal list from logged SignalRecord rows.

Uses the structured levels stored on the row when present; for older rows (or
when levels are missing) it re-parses the raw message text with the offline
regex parser. Only BUY signals with a resolvable symbol are included.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from clonerbot.backtest.engine import BacktestSignal
from clonerbot.db import session_scope
from clonerbot.models.db import SignalRecord
from clonerbot.parser.regex_rules import parse_regex


def _reference_entry(entries: list[float]) -> float:
    return sum(entries) / len(entries) if entries else 0.0


async def load_signals(channel: str | None = None, limit: int = 5000) -> list[BacktestSignal]:
    async with session_scope() as s:
        stmt = select(SignalRecord).order_by(SignalRecord.posted_at.asc()).limit(limit)
        if channel:
            stmt = stmt.where(SignalRecord.channel == channel)
        rows = (await s.execute(stmt)).scalars().all()

    out: list[BacktestSignal] = []
    for r in rows:
        symbol = r.symbol
        entries: list[float] = []
        tps: list[float] = []
        stop = r.stop_loss or 0.0

        if r.entries or r.take_profits or r.stop_loss is not None:
            try:
                entries = json.loads(r.entries) if r.entries else []
                tps = json.loads(r.take_profits) if r.take_profits else []
            except (TypeError, ValueError):
                entries, tps = [], []
        if symbol is None or (not entries and not tps and not stop):
            # Fall back to re-parsing the raw text (offline, regex only).
            rr = parse_regex(r.raw_text or "")
            if rr is None or rr.side != "buy" or not rr.base:
                continue
            symbol = f"{rr.base}/{rr.quote}"
            entries, tps, stop = rr.entries, rr.take_profits, (rr.stop_loss or 0.0)
        if (r.side or "buy") != "buy" or not symbol:
            continue

        out.append(BacktestSignal(
            channel=r.channel,
            symbol=symbol,
            posted_ms=int(r.posted_at.timestamp() * 1000),
            entry=_reference_entry(entries),
            stop_loss=stop,
            take_profit=tps[0] if tps else None,
        ))
    return out
