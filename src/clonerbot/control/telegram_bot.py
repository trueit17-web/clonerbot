"""Telegram control bot (aiogram) — the human's control surface.

Commands (admin-only, restricted to CONTROL_ADMIN_IDS):
  /status    — mode, equity, open positions, daily PnL, kill state
  /positions — list open positions
  /stats     — per-channel reputation and PnL
  /kill      — engage KILL switch and close all positions
  /resume    — clear the KILL switch (does not reopen positions)
  /withdraw <exchange> <asset> <amount> <address>
             — MANUAL withdrawal. Withdrawals are never automatic; this is the
               only way funds leave, and only an admin can invoke it.

Withdrawal note: for safety the trading API keys should have withdrawals
DISABLED. `/withdraw` will attempt a CCXT withdraw and surface the exchange's
response (it will fail cleanly if the key lacks permission), so you keep an
explicit, logged, human-initiated withdrawal path without granting the bot
standing withdrawal power.
"""

from __future__ import annotations

import asyncio

from clonerbot.config import Settings
from clonerbot.exchange.router import ExchangeRouter
from clonerbot.execution.executor import Executor
from clonerbot.logging_conf import get_logger
from clonerbot.scoring.channel_scorer import ChannelScorer

log = get_logger("control")


class ControlBot:
    def __init__(
        self,
        settings: Settings,
        executor: Executor,
        scorer: ChannelScorer,
        router: ExchangeRouter,
        store=None,
        finder=None,
        listener=None,
    ) -> None:
        self._s = settings
        self._executor = executor
        self._scorer = scorer
        self._router = router
        self._store = store        # CandidateStore (discovery); None if disabled
        self._finder = finder      # DiscoveryService
        self._listener = listener  # TelegramListener (for joining)
        self._admins = set(settings.control_admin_ids)
        self._last_join = 0.0      # monotonic timestamp of the last join (cooldown)

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self._admins

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self._admins

    async def run(self, stop: asyncio.Event) -> None:
        from aiogram import Bot, Dispatcher, F
        from aiogram.filters import Command

        bot = Bot(self._s.control_bot_token)
        dp = Dispatcher()

        def guard(handler):
            async def wrapper(message):  # noqa: ANN001
                if not self._is_admin(message.from_user.id):
                    await message.answer("⛔ Not authorized.")
                    return
                await handler(message)
            return wrapper

        @dp.message(Command("status"))
        @guard
        async def _status(message):  # noqa: ANN001
            state = await self._executor.portfolio_state()
            dd = 0.0
            if state.peak_equity > 0:
                dd = (state.peak_equity - state.equity) / state.peak_equity
            await message.answer(
                f"*ClonerBot* ({self._s.mode.value})\n"
                f"Equity: `{state.equity:,.2f}` {self._s.base_quote}\n"
                f"Peak: `{state.peak_equity:,.2f}`  Drawdown: `{dd:.1%}`\n"
                f"Realized today: `{state.realized_pnl_today:,.2f}`\n"
                f"Open positions: `{state.open_count}`\n"
                f"KILL: `{'ON' if state.killed else 'off'}`",
                parse_mode="Markdown",
            )

        @dp.message(Command("positions"))
        @guard
        async def _positions(message):  # noqa: ANN001
            pos = self._executor.open_positions
            if not pos:
                await message.answer("No open positions.")
                return
            lines = [
                f"`{p.symbol}` qty=`{p.qty:.6f}` entry=`{p.entry_price}` "
                f"sl=`{p.stop_loss}` tp=`{p.take_profit}` ({p.channel})"
                for p in pos.values()
            ]
            await message.answer("\n".join(lines), parse_mode="Markdown")

        @dp.message(Command("stats"))
        @guard
        async def _stats(message):  # noqa: ANN001
            rows = await self._scorer.all_stats()
            if not rows:
                await message.answer("No channel stats yet.")
                return
            lines = []
            for r in sorted(rows, key=lambda x: x.cumulative_pnl, reverse=True):
                wr = (r.wins / r.trades_closed) if r.trades_closed else 0.0
                lines.append(
                    f"`{r.channel}` trades=`{r.trades_closed}` wr=`{wr:.0%}` "
                    f"pnl=`{r.cumulative_pnl:,.2f}`"
                )
            await message.answer("\n".join(lines), parse_mode="Markdown")

        @dp.message(Command("kill"))
        @guard
        async def _kill(message):  # noqa: ANN001
            self._executor.killed = True
            n = await self._executor.close_all("kill")
            await message.answer(f"🛑 KILL engaged. Closed {n} position(s). Trading halted.")

        @dp.message(Command("resume"))
        @guard
        async def _resume(message):  # noqa: ANN001
            self._executor.killed = False
            await message.answer("▶️ KILL cleared. Trading resumed.")

        @dp.message(Command("withdraw"))
        @guard
        async def _withdraw(message):  # noqa: ANN001
            parts = (message.text or "").split()
            if len(parts) != 5:
                await message.answer(
                    "Usage: `/withdraw <exchange> <asset> <amount> <address>`",
                    parse_mode="Markdown",
                )
                return
            _, ex_id, asset, amount_s, address = parts
            if self._s.mode.value == "paper":
                await message.answer("Paper mode: withdrawal is simulated (no-op).")
                return
            client = self._router.clients.get(ex_id)
            if client is None:
                await message.answer(f"Unknown exchange `{ex_id}`.", parse_mode="Markdown")
                return
            try:
                amount = float(amount_s)
                ex = client._get()  # narrow, deliberate use of the raw ccxt handle
                result = await ex.withdraw(asset.upper(), amount, address)
                log.info("control.withdraw", exchange=ex_id, asset=asset, amount=amount)
                await message.answer(f"✅ Withdrawal submitted: `{result.get('id', 'ok')}`",
                                     parse_mode="Markdown")
            except Exception as exc:
                await message.answer(f"❌ Withdrawal failed: {exc}")

        # ---------------------------------------------------------- discovery
        @dp.message(Command("discover"))
        @guard
        async def _discover(message):  # noqa: ANN001
            if self._finder is None:
                await message.answer("Discovery is disabled (set DISCOVERY_ENABLED=true).")
                return
            await message.answer("🔎 Scanning for candidate channels…")
            try:
                n = await self._finder.scan_once()
                await message.answer(f"Found {n} new candidate(s). See /candidates.")
            except Exception as exc:
                await message.answer(f"❌ Discovery failed: {exc}")

        @dp.message(Command("candidates"))
        @guard
        async def _candidates(message):  # noqa: ANN001
            if self._store is None:
                await message.answer("Discovery is disabled.")
                return
            rows = await self._store.list_by_status()
            if not rows:
                await message.answer("No candidates yet. Run /discover.")
                return
            order = {"discovered": 0, "observing": 1, "active": 2, "rejected": 3}
            rows.sort(key=lambda r: (order.get(r.status, 9), -r.subscribers))
            lines = []
            for r in rows[:30]:
                mark = {"discovered": "🆕", "observing": "👀", "active": "✅",
                        "rejected": "🚫"}.get(r.status, "•")
                lines.append(f"{mark} `{r.channel}` — {r.status} "
                             f"({r.subscribers:,} subs){' — ' + r.title if r.title else ''}")
            lines.append("\n`/approve @ch` to join+observe · `/reject @ch` to dismiss")
            await message.answer("\n".join(lines), parse_mode="Markdown")

        @dp.message(Command("approve"))
        @guard
        async def _approve(message):  # noqa: ANN001
            if self._store is None or self._listener is None:
                await message.answer("Discovery is disabled.")
                return
            parts = (message.text or "").split()
            if len(parts) != 2:
                await message.answer("Usage: `/approve @channel`", parse_mode="Markdown")
                return
            channel = parts[1] if parts[1].startswith("@") else f"@{parts[1]}"
            cand = await self._store.get(channel)
            if cand is None:
                await message.answer(f"`{channel}` is not a known candidate.",
                                     parse_mode="Markdown")
                return
            # Ban-safety: rate-limit joins.
            import time
            wait = self._s.join_cooldown_sec - (time.monotonic() - self._last_join)
            if wait > 0:
                await message.answer(f"⏳ Join cooldown: wait {int(wait)}s before the next join.")
                return
            try:
                title = await self._listener.join_channel(channel)
                self._last_join = time.monotonic()
                await self._store.approve(channel)
                await message.answer(f"✅ Joined `{channel}` ({title}). Now OBSERVING "
                                     f"(paper-only until it proves out).", parse_mode="Markdown")
            except Exception as exc:
                await message.answer(f"❌ Could not join `{channel}`: {exc}",
                                     parse_mode="Markdown")

        @dp.message(Command("reject"))
        @guard
        async def _reject(message):  # noqa: ANN001
            if self._store is None:
                await message.answer("Discovery is disabled.")
                return
            parts = (message.text or "").split()
            if len(parts) != 2:
                await message.answer("Usage: `/reject @channel`", parse_mode="Markdown")
                return
            channel = parts[1] if parts[1].startswith("@") else f"@{parts[1]}"
            ok = await self._store.reject(channel)
            await message.answer("🚫 Rejected." if ok else "Unknown candidate.")

        @dp.message(F.text == "/start")
        async def _start(message):  # noqa: ANN001
            await message.answer(
                "ClonerBot control. Commands: /status /positions /stats /kill /resume "
                "/withdraw /discover /candidates /approve /reject"
            )

        log.info("control.start", admins=list(self._admins))
        try:
            await dp.start_polling(bot, handle_signals=False)
        except asyncio.CancelledError:
            pass
        finally:
            await bot.session.close()
