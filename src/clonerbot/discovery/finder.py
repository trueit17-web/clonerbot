"""DiscoveryService — find candidate signal channels on Telegram.

Uses Telethon's global search (`contacts.SearchRequest`) over the configured
keywords, keeps public broadcast channels above a subscriber floor, and stores
them as `discovered` candidates for you to approve. It does NOT join anything —
joining happens only after your /approve in the control bot.

Telegram has no "quality" ranking, so this surfaces *candidates* by keyword and
size; trust is earned afterwards in paper via the PromotionService. Runs on an
interval and is a no-op unless DISCOVERY_ENABLED=true.
"""

from __future__ import annotations

import asyncio

from clonerbot.config import Settings
from clonerbot.discovery.store import CandidateStore
from clonerbot.logging_conf import get_logger

log = get_logger("discovery")


class DiscoveryService:
    def __init__(self, settings: Settings, store: CandidateStore, get_client) -> None:
        self._s = settings
        self._store = store
        # get_client: callable returning the live Telethon client (owned by the
        # ingestor) so discovery reuses the same authenticated session.
        self._get_client = get_client

    async def scan_once(self) -> int:
        """Run one search pass across all keywords. Returns count of new candidates."""
        client = self._get_client()
        if client is None:
            log.warning("discovery.no_client")
            return 0

        from telethon.tl.functions.contacts import SearchRequest
        from telethon.tl.types import Channel

        seen_new = 0
        floor = self._s.discovery_min_subscribers
        limit = self._s.discovery_max_candidates_per_scan

        for keyword in self._s.discovery_keywords:
            try:
                res = await client(SearchRequest(q=keyword, limit=limit))
            except Exception as exc:
                log.warning("discovery.search_failed", keyword=keyword, error=str(exc))
                continue

            for chat in getattr(res, "chats", []):
                # Only public broadcast channels (skip megagroups/users/bots).
                if not isinstance(chat, Channel) or not getattr(chat, "broadcast", False):
                    continue
                username = getattr(chat, "username", None)
                if not username:
                    continue  # can't reference/join without a public username
                subs = getattr(chat, "participants_count", 0) or 0
                if subs < floor:
                    continue
                is_new = await self._store.upsert_discovered(
                    channel=f"@{username}", title=getattr(chat, "title", None),
                    subscribers=subs, source=f"search:{keyword}",
                )
                if is_new:
                    seen_new += 1
            await asyncio.sleep(1)  # be gentle with the API between keywords

        log.info("discovery.scan_done", new=seen_new)
        return seen_new

    async def run(self, stop: asyncio.Event) -> None:
        """Periodic scan loop; no-op if discovery is disabled."""
        if not self._s.discovery_enabled:
            log.info("discovery.disabled")
            return
        interval = self._s.discovery_interval_sec
        while not stop.is_set():
            try:
                await self.scan_once()
            except Exception as exc:
                log.warning("discovery.error", error=str(exc))
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
