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

from sqlalchemy import select

from clonerbot.db import session_scope
from clonerbot.models.db import ChannelStats

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

    async def _get_or_create(self, s, channel: str) -> ChannelStats:
        row = await s.get(ChannelStats, channel)
        if row is None:
            # Initialize counters explicitly: column `default=0` is only applied
            # at flush, so a freshly-constructed row would otherwise hold None.
            row = ChannelStats(
                channel=channel, signals_total=0, signals_parsed=0,
                trades_closed=0, wins=0, cumulative_pnl=0.0,
            )
            s.add(row)
        return row

    async def record_signal(self, channel: str, parsed: bool) -> None:
        async with session_scope() as s:
            row = await self._get_or_create(s, channel)
            row.signals_total += 1
            if parsed:
                row.signals_parsed += 1

    async def record_close(self, channel: str, pnl: float) -> None:
        async with session_scope() as s:
            row = await self._get_or_create(s, channel)
            row.trades_closed += 1
            if pnl > 0:
                row.wins += 1
            row.cumulative_pnl += pnl

    async def all_stats(self) -> list[ChannelStats]:
        async with session_scope() as s:
            result = await s.execute(select(ChannelStats))
            return list(result.scalars().all())
