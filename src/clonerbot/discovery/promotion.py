"""PromotionService — moves discovered channels between OBSERVING and ACTIVE
based on their *demonstrated* results, so real money only ever follows channels
that have already proven themselves in paper.

Called after each position closes. Manually configured channels are exempt
(they are ACTIVE by your choice and never auto-demoted here).
"""

from __future__ import annotations

from clonerbot.config import Settings
from clonerbot.db import session_scope
from clonerbot.discovery import ACTIVE, OBSERVING
from clonerbot.discovery.store import CandidateStore
from clonerbot.logging_conf import get_logger
from clonerbot.models.db import ChannelStats

log = get_logger("promotion")


class PromotionService:
    def __init__(
        self,
        settings: Settings,
        store: CandidateStore,
        on_change=None,  # optional async callback(channel, new_status) for notifications
    ) -> None:
        self._s = settings
        self._store = store
        self._on_change = on_change

    async def _stats(self, channel: str) -> tuple[int, int, float]:
        async with session_scope() as s:
            row = await s.get(ChannelStats, channel)
            if row is None:
                return 0, 0, 0.0
            return row.trades_closed, row.wins, row.cumulative_pnl

    async def on_channel_close(self, channel: str) -> None:
        cand = await self._store.get(channel)
        if cand is None or cand.status not in (OBSERVING, ACTIVE):
            return  # manual or rejected/unknown channels aren't managed here

        closed, wins, pnl = await self._stats(channel)
        if closed < self._s.promote_min_trades:
            return
        winrate = wins / closed if closed else 0.0

        if cand.status == OBSERVING:
            if winrate >= self._s.promote_min_winrate and pnl > 0:
                await self._store.promote(channel)
                log.info("promotion.promote", channel=channel, winrate=round(winrate, 3),
                         trades=closed, pnl=round(pnl, 2))
                await self._notify(channel, ACTIVE)
        elif cand.status == ACTIVE:
            if winrate < self._s.demote_winrate:
                await self._store.demote(channel)
                log.info("promotion.demote", channel=channel, winrate=round(winrate, 3),
                         trades=closed)
                await self._notify(channel, OBSERVING)

    async def _notify(self, channel: str, new_status: str) -> None:
        if self._on_change is not None:
            try:
                await self._on_change(channel, new_status)
            except Exception as exc:
                log.warning("promotion.notify_failed", error=str(exc))
