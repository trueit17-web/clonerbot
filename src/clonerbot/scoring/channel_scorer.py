"""Channel reputation scoring.

The system autonomously learns which channels to trust by tracking, per channel,
how its closed trades performed. The score is a size multiplier in [min, max]:
new/unproven channels trade small; channels with a real positive track record
earn larger allocation. This is the closest thing to "picking the best trades"
the bot can honestly do — weight by demonstrated results, not predictions.

The score uses a Laplace-smoothed win rate so a channel needs a *sample* of
closed trades before it can earn a high multiplier (no over-reacting to one win).
"""

from __future__ import annotations

from clonerbot.db import session_scope
from clonerbot.models.db import ChannelStats
from sqlalchemy import select

# A brand-new channel starts here; it grows toward MAX with a proven record.
MIN_MULTIPLIER = 0.5
MAX_MULTIPLIER = 1.5
# Pseudo-counts for Laplace smoothing (prior ~ 50% win rate, weak).
_PRIOR_WINS = 1.0
_PRIOR_TOTAL = 2.0
# Below this many closed trades we stay conservative regardless of win rate.
_MIN_SAMPLE = 5


class ChannelScorer:
    async def multiplier(self, channel: str) -> float:
        """Return a position-size multiplier for the given channel."""
        async with session_scope() as s:
            row = await s.get(ChannelStats, channel)
            if row is None or row.trades_closed < _MIN_SAMPLE:
                return MIN_MULTIPLIER
            smoothed = (row.wins + _PRIOR_WINS) / (row.trades_closed + _PRIOR_TOTAL)
            # Map win rate 0..1 → multiplier MIN..MAX.
            mult = MIN_MULTIPLIER + (MAX_MULTIPLIER - MIN_MULTIPLIER) * smoothed
            return round(max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, mult)), 3)

    async def record_signal(self, channel: str, parsed: bool) -> None:
        async with session_scope() as s:
            row = await s.get(ChannelStats, channel)
            if row is None:
                row = ChannelStats(channel=channel)
                s.add(row)
            row.signals_total += 1
            if parsed:
                row.signals_parsed += 1

    async def record_close(self, channel: str, pnl: float) -> None:
        async with session_scope() as s:
            row = await s.get(ChannelStats, channel)
            if row is None:
                row = ChannelStats(channel=channel)
                s.add(row)
            row.trades_closed += 1
            if pnl > 0:
                row.wins += 1
            row.cumulative_pnl += pnl

    async def all_stats(self) -> list[ChannelStats]:
        async with session_scope() as s:
            result = await s.execute(select(ChannelStats))
            return list(result.scalars().all())
