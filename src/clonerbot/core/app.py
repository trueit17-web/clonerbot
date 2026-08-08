"""Application orchestrator — wires every component and runs the async tasks.

Tasks started concurrently:
  * Telegram ingestor  — raw messages → queue
  * consumer           — queue → pipeline (parse/risk/execute)
  * monitor            — SL/TP checks + equity snapshots
  * control bot        — Telegram commands (status, kill, withdraw)

A shared stop Event coordinates graceful shutdown.
"""

from __future__ import annotations

import asyncio

from clonerbot.config import Settings
from clonerbot.core.pipeline import Pipeline
from clonerbot.core.queue import build_queue
from clonerbot.db import init_db
from clonerbot.exchange.router import ExchangeRouter
from clonerbot.execution.executor import Executor
from clonerbot.ingest.telegram_listener import TelegramListener
from clonerbot.logging_conf import get_logger
from clonerbot.parser.llm_parser import LLMParser
from clonerbot.parser.signal_parser import SignalParser
from clonerbot.risk.risk_engine import RiskEngine
from clonerbot.scoring.channel_scorer import ChannelScorer

log = get_logger("app")


class Application:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.queue = build_queue(settings.redis_url or None)
        self.router = ExchangeRouter(settings)
        self.scorer = ChannelScorer()
        self.parser = SignalParser(LLMParser(), use_llm=bool(settings.anthropic_api_key))
        self.executor = Executor(settings=settings, router=self.router, scorer=self.scorer)
        self.risk = RiskEngine(settings, self.scorer)
        self.listener = TelegramListener(settings, self.queue)
        self._stop = asyncio.Event()

        # --- Channel trust machinery (ALWAYS on) ---
        # The candidate store, gate, shadow executor and promotion service power
        # both discovery AND manual channel-adding via the bot, so they are built
        # unconditionally. Only the periodic auto-search (finder) is gated by
        # DISCOVERY_ENABLED.
        from clonerbot.discovery.finder import DiscoveryService
        from clonerbot.discovery.gate import ChannelGate
        from clonerbot.discovery.promotion import PromotionService
        from clonerbot.discovery.store import CandidateStore

        self.store = CandidateStore()
        self.gate = ChannelGate(settings, self.store)
        promotion = PromotionService(settings, self.store)
        # Shadow executor: OBSERVING channels trade paper-only, and its closes
        # feed the promotion service that graduates proven channels to ACTIVE.
        self.shadow = Executor(
            settings=settings, router=self.router, scorer=self.scorer,
            force_paper=True, promotion=promotion,
        )
        self.finder = (
            DiscoveryService(settings, self.store, lambda: self.listener.client)
            if settings.discovery_enabled else None
        )

        self.pipeline = Pipeline(
            settings, self.parser, self.risk, self.executor, self.scorer,
            gate=self.gate, shadow_executor=self.shadow,
        )

        # Exchange credentials added at runtime via the bot (merged with .env).
        from clonerbot.exchange.credentials import CredentialsStore

        self.creds = CredentialsStore()

    async def _consume(self) -> None:
        while not self._stop.is_set():
            msg = await self.queue.get()
            try:
                await self.pipeline.handle(msg)
            except Exception as exc:
                log.error("consume.error", error=str(exc), key=msg.dedup_key)

    async def run(self) -> None:
        log.info(
            "app.start",
            mode=self.settings.mode.value,
            market=self.settings.market.value,
            exchanges=list(self.settings.exchanges.keys()),
        )
        await init_db()
        # Apply a persisted live/paper override (set via the control bot) so the
        # mode chosen at runtime survives restarts, overriding the .env default.
        from clonerbot.config import Mode
        from clonerbot.core.runtime import MODE_KEY, get_flag

        override = await get_flag(MODE_KEY)
        if override in (Mode.paper.value, Mode.live.value):
            self.settings.mode = Mode(override)
            log.info("app.mode_override", mode=override)
        await self.router.load_stored(self.creds)  # merge bot-added exchange keys
        await self.router.load()
        # Log a clear connectivity summary at startup so "did my keys work?" is
        # answerable straight from the logs.
        for st in await self.router.status_all(self.settings.base_quote):
            log.info(
                "exchange.status", exchange=st.exchange, reachable=st.reachable,
                authenticated=st.authenticated, quote_balance=st.quote_balance,
                tradable=st.tradable, wallets=st.wallets, error=st.error,
            )
        await self.executor.recover_open_positions()

        tasks = [
            asyncio.create_task(self._consume(), name="consumer"),
            asyncio.create_task(self.executor.monitor_loop(self._stop), name="monitor"),
        ]

        # Shadow executor (OBSERVING channels) always runs; discovery scan loop
        # only when the auto-search is enabled.
        await self.shadow.recover_open_positions()
        tasks.append(
            asyncio.create_task(self.shadow.monitor_loop(self._stop), name="shadow_monitor")
        )
        if self.finder is not None:
            tasks.append(asyncio.create_task(self.finder.run(self._stop), name="discovery"))

        # Control bot (optional — only if a token is configured)
        if self.settings.control_bot_token:
            from clonerbot.control.telegram_bot import ControlBot

            self.control = ControlBot(
                self.settings, self.executor, self.scorer, self.router,
                store=self.store, finder=self.finder, listener=self.listener,
                creds=self.creds,
            )
            tasks.append(asyncio.create_task(self.control.run(self._stop), name="control"))

        # Telegram ingest: always listen broadly and filter via the gate, so
        # both configured channels and channels added/joined later are picked up.
        if self.settings.tg_api_id:
            tasks.append(asyncio.create_task(self.listener.start(gate=self.gate), name="ingest"))
        else:
            log.warning("app.no_ingest", reason="TG not configured; queue must be fed manually")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown(tasks)

    async def shutdown(self, tasks: list[asyncio.Task]) -> None:
        log.info("app.shutdown")
        self._stop.set()
        await self.listener.stop()
        for t in tasks:
            t.cancel()
        await self.router.close()
        await self.queue.close()
