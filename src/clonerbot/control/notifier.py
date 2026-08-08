"""Push notifications to the operator's Telegram.

A lightweight sender (its own aiogram Bot, independent of the control bot's
polling dispatcher) used to surface live activity — paper/live trades opening
and closing, and channel promotions — so the bot's paper work is visible in real
time instead of only when you press a button.
"""

from __future__ import annotations

from clonerbot.logging_conf import get_logger

log = get_logger("notifier")


class Notifier:
    def __init__(self, token: str | None, admin_ids: list[int]) -> None:
        self._token = token
        self._admins = list(admin_ids)
        self._bot = None

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._admins)

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        if self._bot is None:
            from aiogram import Bot

            self._bot = Bot(self._token)
        for uid in self._admins:
            try:
                await self._bot.send_message(uid, text, parse_mode="HTML")
            except Exception as exc:
                log.warning("notifier.send_failed", uid=uid, error=str(exc))

    async def close(self) -> None:
        if self._bot is not None:
            try:
                await self._bot.session.close()
            except Exception:
                pass

    # Allow the instance to be used directly as `await notifier(text)`.
    async def __call__(self, text: str) -> None:
        await self.send(text)
