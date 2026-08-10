"""Protections — freqtrade-style safety locks that pause trading after bad runs.

Implemented protections:
  * CooldownPeriod — after any close on a channel, don't re-enter it briefly.
  * StoplossGuard  — too many stop-loss exits in a window → pause ALL trading.
  * LosingStreak   — N consecutive losing closes on a channel → lock that channel.

Locks are time-bounded rows in `pair_locks`, keyed by scope ("global",
"channel:<name>", "symbol:<SYM>"). The risk engine refuses to open while any of
a signal's keys is locked. Locks persist across restarts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from clonerbot.config import Settings
from clonerbot.db import session_scope
from clonerbot.logging_conf import get_logger
from clonerbot.models.db import PairLock, Position

log = get_logger("protections")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def channel_key(channel: str) -> str:
    return f"channel:{channel}"


def symbol_key(symbol: str) -> str:
    return f"symbol:{symbol}"


GLOBAL_KEY = "global"


class LockStore:
    async def add(self, scope_key: str, minutes: int, reason: str) -> None:
        if minutes <= 0:
            return
        async with session_scope() as s:
            s.add(PairLock(scope_key=scope_key, reason=reason,
                           until=_now() + timedelta(minutes=minutes)))
        log.info("lock.add", scope=scope_key, minutes=minutes, reason=reason)

    async def active_reason(self, keys: list[str]) -> str | None:
        """Return the reason of an active lock matching any of `keys`, else None."""
        async with session_scope() as s:
            row = (
                await s.execute(
                    select(PairLock)
                    .where(PairLock.scope_key.in_(keys), PairLock.until > _now())
                    .order_by(PairLock.until.desc())
                )
            ).scalars().first()
            return row.reason if row else None

    async def clear_expired(self) -> int:
        async with session_scope() as s:
            rows = (
                await s.execute(select(PairLock).where(PairLock.until <= _now()))
            ).scalars().all()
            for r in rows:
                await s.delete(r)
            return len(rows)


class ProtectionManager:
    def __init__(self, settings: Settings, locks: LockStore) -> None:
        self._s = settings
        self._locks = locks

    async def on_close(self, channel: str, symbol: str, pnl: float, reason: str) -> None:
        if not self._s.protections_enabled:
            return
        # 1) Cooldown after any close on this channel.
        await self._locks.add(channel_key(channel), self._s.cooldown_minutes,
                              "cooldown after trade")
        # 2) Stoploss guard (global) — react to a cluster of stop-losses.
        if reason == "sl":
            await self._maybe_stoploss_guard()
        # 3) Losing streak on this channel.
        if pnl < 0:
            await self._maybe_losing_streak(channel)

    async def _maybe_stoploss_guard(self) -> None:
        s = self._s
        since = _now() - timedelta(minutes=s.stoploss_guard_window_min)
        async with session_scope() as sess:
            n = len((
                await sess.execute(
                    select(Position.id).where(
                        Position.status == "closed",
                        Position.close_reason == "sl",
                        Position.closed_at >= since,
                    )
                )
            ).scalars().all())
        if n >= s.stoploss_guard_count:
            await self._locks.add(GLOBAL_KEY, s.stoploss_guard_lock_min,
                                  f"stoploss guard: {n} SL in {s.stoploss_guard_window_min}m")

    async def _maybe_losing_streak(self, channel: str) -> None:
        s = self._s
        async with session_scope() as sess:
            rows = (
                await sess.execute(
                    select(Position.realized_pnl)
                    .where(Position.status == "closed", Position.channel == channel)
                    .order_by(Position.closed_at.desc())
                    .limit(s.losing_streak_count)
                )
            ).scalars().all()
        if len(rows) >= s.losing_streak_count and all((p or 0) < 0 for p in rows):
            await self._locks.add(channel_key(channel), s.losing_streak_lock_min,
                                  f"losing streak: {s.losing_streak_count} losses")
