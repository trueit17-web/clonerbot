"""ChannelGate — the single authority on how much a channel is trusted.

Two kinds of channels:
  * Manually configured (settings.tg_channels) — trusted as ACTIVE from the
    start; you chose them yourself.
  * Discovered — start OBSERVING (paper-only) and are promoted to ACTIVE by the
    PromotionService once they prove out. Reflected in the candidates table.

The gate answers two questions used across the pipeline:
  * is_ingesting(channel): should we parse this channel's messages at all?
  * trades_real(channel): may this channel place REAL orders (vs shadow/paper)?
"""

from __future__ import annotations

from clonerbot.config import Settings
from clonerbot.discovery import ACTIVE, INGESTING_STATUSES
from clonerbot.discovery.store import CandidateStore


class ChannelGate:
    def __init__(self, settings: Settings, store: CandidateStore) -> None:
        self._manual = {c.strip() for c in settings.tg_channels}
        self._store = store

    def is_manual(self, channel: str) -> bool:
        return channel in self._manual

    async def status(self, channel: str) -> str | None:
        if channel in self._manual:
            return ACTIVE
        cand = await self._store.get(channel)
        return cand.status if cand else None

    async def is_ingesting(self, channel: str) -> bool:
        """True for manually configured channels and approved discovered ones."""
        if channel in self._manual:
            return True
        cand = await self._store.get(channel)
        return bool(cand and cand.status in INGESTING_STATUSES)

    async def trades_real(self, channel: str) -> bool:
        """True only for ACTIVE channels; OBSERVING ones are shadow (paper)."""
        return (await self.status(channel)) == ACTIVE
