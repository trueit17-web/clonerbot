"""Telegram ingestor using Telethon.

Listens to the configured channels with a USER session (bots cannot read
arbitrary channels) and pushes each new message onto the shared queue as a
RawMessage. Parsing/decisions happen downstream — this component only ingests.

First run is interactive: Telethon will prompt for your phone number and the
login code. The session is then persisted (CLONERBOT_TG_SESSION) so subsequent
runs are non-interactive.
"""

from __future__ import annotations

from datetime import timezone

from clonerbot.config import Settings
from clonerbot.core.queue import MessageQueue
from clonerbot.logging_conf import get_logger
from clonerbot.models.signal import RawMessage

log = get_logger("telegram")


class TelegramListener:
    def __init__(self, settings: Settings, queue: MessageQueue | None = None) -> None:
        self._settings = settings
        self._queue = queue
        self._client = None

    def _build_client(self):
        from telethon import TelegramClient

        s = self._settings
        if not (s.tg_api_id and s.tg_api_hash):
            raise RuntimeError("TG_API_ID / TG_API_HASH not configured")
        return TelegramClient(s.tg_session, s.tg_api_id, s.tg_api_hash)

    async def login(self) -> None:
        """One-time interactive Telegram login.

        Connects (prompting for phone + code on first run), persists the session
        file, and verifies access to each configured channel — then disconnects.
        This is the clean first-run step: no pipeline, no orders, just auth.
        """
        client = self._build_client()
        await client.start()  # interactive prompts on first run only
        try:
            me = await client.get_me()
            handle = f"@{me.username}" if getattr(me, "username", None) else str(me.id)
            log.info("telegram.login_ok", account=handle)
            print(f"✅ Logged in as {handle}. Session saved to: {self._settings.tg_session}")

            channels = self._settings.tg_channels
            if not channels:
                print("⚠️  No TG_CHANNELS configured yet — add them to .env before `run`.")
                return
            print("Checking channel access:")
            for c in channels:
                c = c.strip()
                target = int(c) if c.lstrip("-").isdigit() else c
                try:
                    entity = await client.get_entity(target)
                    title = getattr(entity, "title", None) or getattr(entity, "username", c)
                    print(f"  ✅ {c} → {title}")
                except Exception as exc:
                    print(f"  ❌ {c} → not reachable ({exc}). Join it with this account first.")
        finally:
            await client.disconnect()

    @staticmethod
    def _normalize_channel(chat) -> str:
        username = getattr(chat, "username", None)
        if username:
            return f"@{username}"
        return str(getattr(chat, "id", "unknown"))

    @property
    def client(self):
        """The live Telethon client (None until start()); used by discovery/join."""
        return self._client

    async def join_channel(self, username: str) -> str:
        """Join a public channel by @username. Returns its title. Raises on failure."""
        if self._client is None:
            raise RuntimeError("Telegram client not started")
        from telethon.tl.functions.channels import JoinChannelRequest

        entity = await self._client.get_entity(username)
        await self._client(JoinChannelRequest(entity))
        title = getattr(entity, "title", None) or username
        log.info("telegram.joined", channel=username, title=title)
        return title

    async def start(self, gate=None) -> None:
        """Connect, register the handler, and run until disconnected.

        With a `gate` (discovery mode) we listen to ALL channels and filter each
        message by gate.is_ingesting(), so channels joined later are picked up
        without re-registering handlers. Without a gate we filter to the fixed
        configured channels (legacy, more efficient).
        """
        from telethon import events

        channels = self._settings.tg_channels
        self._client = self._build_client()
        await self._client.start()  # interactive on first run only
        log.info("telegram.connected", channels=channels, discovery=gate is not None)

        async def _dispatch(event, channel: str) -> None:  # noqa: ANN001
            text = event.message.message or ""
            if not text.strip():
                return
            posted = event.message.date
            if posted and posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            raw = RawMessage(
                channel=channel, message_id=event.message.id, text=text, posted_at=posted
            )
            accepted = await self._queue.put(raw)
            log.info(
                "telegram.message", channel=raw.channel, message_id=raw.message_id,
                queued=accepted, preview=text[:80].replace("\n", " "),
            )

        if gate is None:
            if not channels:
                raise RuntimeError("No TG_CHANNELS configured")
            stripped = (x.strip() for x in channels)
            targets = [int(c) if c.lstrip("-").isdigit() else c for c in stripped]

            @self._client.on(events.NewMessage(chats=targets))
            async def _fixed(event) -> None:  # noqa: ANN001
                await _dispatch(event, self._normalize_channel(await event.get_chat()))
        else:
            @self._client.on(events.NewMessage())
            async def _dynamic(event) -> None:  # noqa: ANN001
                if not getattr(event, "is_channel", False):
                    return  # ignore DMs/groups; we only trade channel signals
                channel = self._normalize_channel(await event.get_chat())
                if not await gate.is_ingesting(channel):
                    return  # not an approved/observing channel → skip
                await _dispatch(event, channel)

        await self._client.run_until_disconnected()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            log.info("telegram.disconnected")
