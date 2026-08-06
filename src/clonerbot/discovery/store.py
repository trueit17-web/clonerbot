"""CandidateStore — persistence and lifecycle transitions for discovered channels."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from clonerbot.db import session_scope
from clonerbot.discovery import ACTIVE, DISCOVERED, OBSERVING, REJECTED
from clonerbot.logging_conf import get_logger
from clonerbot.models.db import ChannelCandidate

log = get_logger("candidates")


@dataclass
class CandidateView:
    """Detached snapshot so callers don't hold ORM objects across sessions."""

    channel: str
    title: str | None
    subscribers: int
    status: str
    joined: bool


def _view(row: ChannelCandidate) -> CandidateView:
    return CandidateView(
        channel=row.channel, title=row.title, subscribers=row.subscribers,
        status=row.status, joined=row.joined,
    )


class CandidateStore:
    async def upsert_discovered(
        self, channel: str, title: str | None, subscribers: int, source: str = "search"
    ) -> bool:
        """Insert a newly found channel as `discovered`. Returns True if new.

        Never resurrects a rejected channel and never downgrades an approved one.
        """
        async with session_scope() as s:
            row = await s.get(ChannelCandidate, channel)
            if row is not None:
                # Refresh metadata but leave the lifecycle status alone.
                row.title = title or row.title
                if subscribers:
                    row.subscribers = subscribers
                return False
            s.add(ChannelCandidate(
                channel=channel, title=title, subscribers=subscribers,
                source=source, status=DISCOVERED,
            ))
            log.info("candidate.discovered", channel=channel, subscribers=subscribers)
            return True

    async def list_by_status(self, status: str | None = None) -> list[CandidateView]:
        async with session_scope() as s:
            stmt = select(ChannelCandidate)
            if status is not None:
                stmt = stmt.where(ChannelCandidate.status == status)
            rows = (await s.execute(stmt)).scalars().all()
            return [_view(r) for r in rows]

    async def get(self, channel: str) -> CandidateView | None:
        async with session_scope() as s:
            row = await s.get(ChannelCandidate, channel)
            return _view(row) if row else None

    async def set_status(self, channel: str, status: str, joined: bool | None = None) -> bool:
        async with session_scope() as s:
            row = await s.get(ChannelCandidate, channel)
            if row is None:
                return False
            row.status = status
            if joined is not None:
                row.joined = joined
            log.info("candidate.status", channel=channel, status=status)
            return True

    async def approve(self, channel: str) -> bool:
        """Move discovered → observing (called after a successful join)."""
        return await self.set_status(channel, OBSERVING, joined=True)

    async def reject(self, channel: str) -> bool:
        return await self.set_status(channel, REJECTED)

    async def promote(self, channel: str) -> bool:
        return await self.set_status(channel, ACTIVE)

    async def demote(self, channel: str) -> bool:
        return await self.set_status(channel, OBSERVING)
