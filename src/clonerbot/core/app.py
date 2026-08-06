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
        self.pipeline = Pipeline(settings, self.parser, self.risk, self.executor, self.scorer)
        self.listener = TelegramListener(settings, self.queue)
        self._stop = asyncio.Event()

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
        await self.router.load()
        await self.executor.recover_open_positions()

        tasks = [
            asyncio.create_task(self._consume(), name="consumer"),
            asyncio.create_task(self.executor.monitor_loop(self._stop), name="monitor"),
        ]

        # Control bot (optional — only if a token is configured)
        if self.settings.control_bot_token:
            from clonerbot.control.telegram_bot import ControlBot

            self.control = ControlBot(self.settings, self.executor, self.scorer, self.router)
            tasks.append(asyncio.create_task(self.control.run(self._stop), name="control"))

        # Telegram ingest (optional — only if configured; paper demos may omit it)
        if self.settings.tg_api_id and self.settings.tg_channels:
            tasks.append(asyncio.create_task(self.listener.start(), name="ingest"))
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
