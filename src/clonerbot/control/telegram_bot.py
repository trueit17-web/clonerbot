"""Telegram control bot (aiogram) — the operator's control surface.

Button-driven, Russian UI. A persistent bottom menu groups the common actions;
destructive and per-candidate actions use inline buttons (with confirmation for
the emergency stop, and ✅/🚫 for channel approval). Access is restricted to
CONTROL_ADMIN_IDS.

Withdrawal note: for safety the trading API keys should have withdrawals
DISABLED. Withdrawal is a typed command (it needs an address/amount) and simply
surfaces the exchange's response — an explicit, logged, human-initiated path
without granting the bot standing withdrawal power.
"""

from __future__ import annotations

import asyncio
import html
import time

from clonerbot.config import Settings
from clonerbot.control import keyboards as kb
from clonerbot.exchange.router import ExchangeRouter
from clonerbot.execution.executor import Executor
from clonerbot.logging_conf import get_logger
from clonerbot.scoring.channel_scorer import ChannelScorer

log = get_logger("control")


def _esc(s: object) -> str:
    return html.escape(str(s))


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

    # ------------------------------------------------------------------ content
    async def status_text(self) -> str:
        st = await self._executor.portfolio_state()
        dd = (st.peak_equity - st.equity) / st.peak_equity if st.peak_equity > 0 else 0.0
        mode = "🧪 paper" if self._executor.is_paper else "🔴 LIVE"
        kill = "🛑 ВКЛ" if st.killed else "✅ выкл"
        q = self._s.base_quote
        return (
            f"<b>ClonerBot</b> — режим: {mode}\n"
            f"💰 Капитал: <b>{st.equity:,.2f}</b> {q}\n"
            f"⛰ Пик: {st.peak_equity:,.2f} · Просадка: {dd:.1%}\n"
            f"📅 PnL за сегодня: <b>{st.realized_pnl_today:,.2f}</b> {q}\n"
            f"📌 Открытых позиций: <b>{st.open_count}</b>\n"
            f"🚨 Аварийный стоп: {kill}"
        )

    def positions_text(self) -> str:
        pos = self._executor.open_positions
        if not pos:
            return "📈 Открытых позиций нет."
        lines = ["<b>📈 Открытые позиции</b>"]
        for p in pos.values():
            lines.append(
                f"• <b>{_esc(p.symbol)}</b> — {p.qty:.6f} @ {p.entry_price:g}\n"
                f"   🛑 стоп {p.stop_loss:g} · 🎯 тейк {p.take_profit if p.take_profit else '—'}"
                f" · 📡 {_esc(p.channel)}"
            )
        return "\n".join(lines)

    async def rating_text(self) -> str:
        rows = await self._scorer.all_stats()
        if not rows:
            return "🏆 Статистики по каналам пока нет."
        rows.sort(key=lambda x: x.cumulative_pnl, reverse=True)
        lines = ["<b>🏆 Рейтинг каналов</b> (по суммарному PnL)"]
        for r in rows[:20]:
            wr = (r.wins / r.trades_closed) if r.trades_closed else 0.0
            medal = "🟢" if r.cumulative_pnl > 0 else "🔴"
            lines.append(
                f"{medal} <b>{_esc(r.channel)}</b> — сделок {r.trades_closed}, "
                f"winrate {wr:.0%}, PnL {r.cumulative_pnl:,.2f}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ run
    async def run(self, stop: asyncio.Event) -> None:
        from aiogram import Bot, Dispatcher, F
        from aiogram.filters import Command

        bot = Bot(self._s.control_bot_token)
        dp = Dispatcher()
        menu = kb.build_main_menu(self._s.discovery_enabled)

        async def deny(msg_or_cb) -> bool:
            """True (and notifies) if the user is not an admin."""
            uid = msg_or_cb.from_user.id
            if self._is_admin(uid):
                return False
            if hasattr(msg_or_cb, "answer"):
                await msg_or_cb.answer("⛔ Нет доступа.")
            return True

        # ----- entry / help -----
        async def _send_menu(message) -> None:  # noqa: ANN001
            await message.answer(
                "🤖 <b>Панель управления ClonerBot</b>\nВыберите действие в меню ниже.",
                reply_markup=menu, parse_mode="HTML",
            )

        @dp.message(Command("start", "menu"))
        async def _start(message):  # noqa: ANN001
            if await deny(message):
                return
            await _send_menu(message)

        @dp.message(F.text == kb.BTN_HELP)
        async def _help(message):  # noqa: ANN001
            if await deny(message):
                return
            await message.answer(
                "ℹ️ <b>Справка</b>\n\n"
                "📊 Статус — капитал, PnL, просадка, стоп\n"
                "📈 Позиции — открытые сделки\n"
                "🏆 Рейтинг каналов — winrate и PnL по каналам\n"
                "🔎 Искать каналы — запустить поиск сигнальных каналов\n"
                "📋 Кандидаты — одобрить/отклонить найденные каналы (✅/🚫)\n"
                "🛑 Стоп-торговля — аварийно закрыть всё (с подтверждением)\n"
                "▶️ Возобновить — снять аварийный стоп\n"
                "💸 Вывод средств — ручной вывод (ввод командой)\n\n"
                "Новые каналы сначала торгуют <b>только на бумаге</b> и допускаются "
                "к деньгам лишь после подтверждённого профита.",
                reply_markup=menu, parse_mode="HTML",
            )

        # ----- info actions -----
        @dp.message(F.text == kb.BTN_STATUS)
        async def _status(message):  # noqa: ANN001
            if await deny(message):
                return
            await message.answer(await self.status_text(), parse_mode="HTML")

        @dp.message(F.text == kb.BTN_POSITIONS)
        async def _positions(message):  # noqa: ANN001
            if await deny(message):
                return
            await message.answer(self.positions_text(), parse_mode="HTML")

        @dp.message(F.text == kb.BTN_RATING)
        async def _rating(message):  # noqa: ANN001
            if await deny(message):
                return
            await message.answer(await self.rating_text(), parse_mode="HTML")

        # ----- emergency stop (with confirmation) -----
        @dp.message(F.text == kb.BTN_KILL)
        async def _kill(message):  # noqa: ANN001
            if await deny(message):
                return
            await message.answer(
                "⚠️ <b>Остановить торговлю и закрыть все позиции?</b>\nЭто действие немедленно.",
                reply_markup=kb.build_kill_confirm_kb(), parse_mode="HTML",
            )

        @dp.callback_query(F.data == kb.CB_KILL_YES)
        async def _kill_yes(cb):  # noqa: ANN001
            if await deny(cb):
                return
            self._executor.killed = True
            n = await self._executor.close_all("kill")
            await cb.message.edit_text(f"🛑 Торговля остановлена. Закрыто позиций: {n}.")
            await cb.answer("Остановлено")

        @dp.callback_query(F.data == kb.CB_KILL_NO)
        async def _kill_no(cb):  # noqa: ANN001
            if await deny(cb):
                return
            await cb.message.edit_text("Отменено. Торговля продолжается.")
            await cb.answer()

        @dp.message(F.text == kb.BTN_RESUME)
        async def _resume(message):  # noqa: ANN001
            if await deny(message):
                return
            self._executor.killed = False
            await message.answer("▶️ Аварийный стоп снят. Торговля возобновлена.")

        # ----- withdrawal (typed; needs address/amount) -----
        @dp.message(F.text == kb.BTN_WITHDRAW)
        async def _withdraw_help(message):  # noqa: ANN001
            if await deny(message):
                return
            await message.answer(
                "💸 <b>Вывод средств</b>\nОтправьте команду в формате:\n"
                "<code>/withdraw биржа монета сумма адрес</code>\n"
                "Пример: <code>/withdraw bybit USDT 100 TАдресКошелька</code>",
                parse_mode="HTML",
            )

        @dp.message(Command("withdraw"))
        async def _withdraw(message):  # noqa: ANN001
            if await deny(message):
                return
            parts = (message.text or "").split()
            if len(parts) != 5:
                await message.answer("Формат: /withdraw биржа монета сумма адрес")
                return
            _, ex_id, asset, amount_s, address = parts
            if self._s.mode.value == "paper":
                await message.answer("🧪 Paper-режим: вывод имитируется (ничего не отправлено).")
                return
            client = self._router.clients.get(ex_id)
            if client is None:
                await message.answer(f"Неизвестная биржа: {_esc(ex_id)}")
                return
            try:
                amount = float(amount_s)
                ex = client._get()
                result = await ex.withdraw(asset.upper(), amount, address)
                log.info("control.withdraw", exchange=ex_id, asset=asset, amount=amount)
                rid = _esc(result.get("id", "ok"))
                await message.answer(f"✅ Заявка на вывод отправлена: {rid}")
            except Exception as exc:
                await message.answer(f"❌ Ошибка вывода: {_esc(exc)}")

        # ----- discovery -----
        @dp.message(F.text == kb.BTN_DISCOVER)
        async def _discover(message):  # noqa: ANN001
            if await deny(message):
                return
            if self._finder is None:
                await message.answer("🔎 Поиск каналов выключен (DISCOVERY_ENABLED=false).")
                return
            await message.answer("🔎 Ищу сигнальные каналы…")
            try:
                n = await self._finder.scan_once()
                await message.answer(f"Готово. Новых кандидатов: {n}. Откройте «📋 Кандидаты».")
            except Exception as exc:
                await message.answer(f"❌ Ошибка поиска: {_esc(exc)}")

        @dp.message(F.text == kb.BTN_CANDIDATES)
        async def _candidates(message):  # noqa: ANN001
            if await deny(message):
                return
            if self._store is None:
                await message.answer("Поиск каналов выключен.")
                return
            rows = await self._store.list_by_status()
            if not rows:
                await message.answer("Кандидатов пока нет. Нажмите «🔎 Искать каналы».")
                return
            order = {"discovered": 0, "observing": 1, "active": 2, "rejected": 3}
            rows.sort(key=lambda r: (order.get(r.status, 9), -r.subscribers))
            ru = {"discovered": "🆕 новый", "observing": "👀 наблюдение (paper)",
                  "active": "✅ активный", "rejected": "🚫 отклонён"}
            shown = 0
            for r in rows:
                if r.status == "rejected":
                    continue
                title = f" — {_esc(r.title)}" if r.title else ""
                text = (f"📡 <b>{_esc(r.channel)}</b>{title}\n"
                        f"👥 {r.subscribers:,} подписчиков · статус: {ru.get(r.status, r.status)}")
                # Approve/reject buttons only for undecided candidates.
                markup = kb.build_candidate_kb(r.channel) if r.status == "discovered" else None
                await message.answer(text, reply_markup=markup, parse_mode="HTML")
                shown += 1
                if shown >= 20:
                    break
            if shown == 0:
                await message.answer("Активных кандидатов нет.")

        @dp.callback_query(F.data.startswith(kb.CB_APPROVE))
        async def _approve(cb):  # noqa: ANN001
            if await deny(cb):
                return
            channel = cb.data[len(kb.CB_APPROVE):]
            if self._store is None or self._listener is None:
                await cb.answer("Поиск каналов выключен.", show_alert=True)
                return
            wait = self._s.join_cooldown_sec - (time.monotonic() - self._last_join)
            if wait > 0:
                await cb.answer(f"Пауза между вступлениями: подождите {int(wait)} с.",
                                show_alert=True)
                return
            try:
                title = await self._listener.join_channel(channel)
                self._last_join = time.monotonic()
                await self._store.approve(channel)
                await cb.message.edit_text(
                    f"✅ Вступил в <b>{_esc(channel)}</b> ({_esc(title)}).\n"
                    f"👀 Наблюдение — торгует только на бумаге до подтверждения профита.",
                    parse_mode="HTML",
                )
                await cb.answer("Одобрено")
            except Exception as exc:
                await cb.answer(f"Не удалось вступить: {exc}", show_alert=True)

        @dp.callback_query(F.data.startswith(kb.CB_REJECT))
        async def _reject(cb):  # noqa: ANN001
            if await deny(cb):
                return
            channel = cb.data[len(kb.CB_REJECT):]
            if self._store is None:
                await cb.answer("Поиск каналов выключен.", show_alert=True)
                return
            await self._store.reject(channel)
            await cb.message.edit_text(f"🚫 Канал <b>{_esc(channel)}</b> отклонён.",
                                       parse_mode="HTML")
            await cb.answer("Отклонён")

        log.info("control.start", admins=list(self._admins))
        try:
            await dp.start_polling(bot, handle_signals=False)
        except asyncio.CancelledError:
            pass
        finally:
            await bot.session.close()
