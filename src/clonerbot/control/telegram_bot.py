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

from sqlalchemy import select

from clonerbot.config import Mode, Settings
from clonerbot.control import keyboards as kb
from clonerbot.core.runtime import MODE_KEY, set_flag
from clonerbot.db import session_scope
from clonerbot.exchange.router import ExchangeRouter
from clonerbot.execution.executor import Executor
from clonerbot.logging_conf import get_logger
from clonerbot.models.db import Position
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
        creds=None,
    ) -> None:
        self._s = settings
        self._executor = executor
        self._scorer = scorer
        self._router = router
        self._store = store        # CandidateStore (discovery); None if disabled
        self._finder = finder      # DiscoveryService
        self._listener = listener  # TelegramListener (for joining)
        self._creds = creds        # CredentialsStore (bot-managed exchange keys)
        self._admins = set(settings.control_admin_ids)
        # monotonic time of the last join, or None if we haven't joined yet.
        # (None avoids a false cooldown on the first join, since monotonic()'s
        # zero point is arbitrary and can be smaller than the cooldown.)
        self._last_join: float | None = None
        self._pending: dict[int, str] = {}  # user_id → awaited free-text action

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self._admins

    def _join_wait(self) -> float:
        """Seconds to wait before the next join (0 if allowed now)."""
        if self._last_join is None:
            return 0.0
        return self._s.join_cooldown_sec - (time.monotonic() - self._last_join)

    # ------------------------------------------------------------------ actions
    async def add_channel(self, channel: str) -> str:
        """Join a channel and start OBSERVING it (paper-first). Returns a status text.

        Manually added channels follow the same safe path as discovered ones:
        they trade on paper until they prove out, then auto-promote. This keeps a
        typo'd or over-hyped channel from touching real money on day one.
        """
        if self._listener is None:
            return "Приём Telegram не запущен — добавить канал сейчас нельзя."
        channel = channel.strip()
        if not channel.startswith("@"):
            channel = "@" + channel.lstrip("@")
        if " " in channel or len(channel) < 3:
            return "Некорректное имя канала. Пример: @channelname"
        wait = self._join_wait()
        if wait > 0:
            return f"⏳ Пауза между вступлениями: подождите {int(wait)} с."
        try:
            title = await self._listener.join_channel(channel)
        except Exception as exc:
            return f"❌ Не удалось вступить в {channel}: {exc}"
        self._last_join = time.monotonic()
        if self._store is not None:
            await self._store.upsert_discovered(channel, title, 0, source="manual")
            await self._store.approve(channel)
        return (f"✅ Добавлен и подключён {channel} ({title}).\n"
                f"👀 Наблюдение — торгует на бумаге до подтверждения профита, "
                f"затем авто-допуск к деньгам.")

    async def exchange_status_text(self) -> str:
        """Human-readable connection status for every configured exchange."""
        if not self._router.has_exchanges:
            return ("🔌 <b>Биржи</b>\nНи одна биржа не подключена.\n"
                    "Нажмите «➕ Добавить биржу», чтобы задать API-ключи.")
        statuses = await self._router.status_all(self._s.base_quote)
        mode = "🧪 paper (реальных ордеров нет)" if self._executor.is_paper else "🔴 LIVE"
        lines = [f"🔌 <b>Статус бирж</b> · режим: {mode}"]
        for st in statuses:
            if not st.reachable:
                lines.append(f"❌ <b>{_esc(st.exchange)}</b> — недоступна ({_esc(st.error)})")
            elif not st.authenticated:
                lines.append(
                    f"🟡 <b>{_esc(st.exchange)}</b> — подключена, но ключи не работают "
                    f"({_esc(st.error)})"
                )
            else:
                spot = "спот ✅" if st.spot else "спот ❌"
                q = self._s.base_quote
                lines.append(
                    f"✅ <b>{_esc(st.exchange)}</b> — ключи ок · {spot} · "
                    f"всего {q}: {st.quote_balance:,.2f}"
                )
                lines.append(f"   🟢 Доступно для торговли: <b>{st.tradable:,.2f}</b> {q}")
                if st.wallets:
                    lines.append(f"   💼 кошельки: {_esc(st.wallets)}")
                if st.tradable == 0 and (st.quote_balance > 0 or st.wallets):
                    lines.append("   ⚠️ Средства есть, но не на спотовом торговом счёте — "
                                 f"переведите их в спот {q}, чтобы бот мог торговать.")
                elif st.tradable == 0 and not st.wallets:
                    lines.append("   💼 ненулевых балансов не найдено — проверьте счёт и монету.")
        if self._executor.is_paper:
            lines.append("\nℹ️ Сейчас paper-режим: сделки считаются на бумаге. "
                         "Для реальной торговли поставьте CLONERBOT_MODE=live.")
        return "\n".join(lines)

    async def add_exchange(self, exchange_id: str, text: str) -> str:
        """Save API creds for an exchange, wire it in, and test the connection."""
        from clonerbot.exchange.credentials import parse_credentials

        parsed = parse_credentials(text)
        if parsed is None:
            return ("Не разобрал ключи. Пришлите одним сообщением:\n"
                    "<code>API_KEY SECRET</code> (и passphrase, если нужен).")
        api_key, secret, password = parsed
        if self._creds is not None:
            await self._creds.upsert(exchange_id, api_key, secret, password)
        creds = {"apiKey": api_key, "secret": secret}
        if password:
            creds["password"] = password
        self._router.add_client(exchange_id, creds)
        # Test right away so the user gets immediate confirmation.
        try:
            st = await self._router.clients[exchange_id].check(self._s.base_quote)
        except Exception as exc:
            return f"Ключи сохранены, но проверка не удалась: {_esc(exc)}"
        if st.authenticated:
            return (f"✅ <b>{_esc(exchange_id)}</b> подключена, ключи работают.\n"
                    f"Баланс: {st.quote_balance:,.2f} {self._s.base_quote}\n"
                    f"⚠️ Удалите сообщение с ключами из чата для безопасности.")
        if st.reachable:
            return (f"🟡 Биржа доступна, но ключи не прошли проверку: {_esc(st.error)}\n"
                    f"Проверьте права ключа (нужен спот) и повторите.")
        return f"❌ Биржа недоступна: {_esc(st.error)}"

    async def remove_exchange(self, exchange_id: str) -> str:
        ok = await self._router.remove_client(exchange_id)
        if self._creds is not None:
            await self._creds.delete(exchange_id)
        return (f"🗑 Биржа <b>{_esc(exchange_id)}</b> удалена." if ok
                else f"Биржа {_esc(exchange_id)} не найдена.")

    async def set_mode(self, live: bool) -> str:
        """Switch live/paper at runtime and persist the choice across restarts."""
        self._s.mode = Mode.live if live else Mode.paper
        await set_flag(MODE_KEY, self._s.mode.value)
        log.info("control.mode_switch", mode=self._s.mode.value)
        if live:
            return ("🔴 Включён <b>LIVE</b> — сделки идут на реальные деньги.\n"
                    "Проверьте лимиты риска и баланс на бирже.")
        return "🧪 Возвращён <b>paper</b> — сделки считаются на бумаге."

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _upnl(pos, price: float) -> float:
        """Unrealized PnL in quote for an open position at the given price."""
        return (pos.qty * (price - pos.entry_price) if pos.is_long
                else pos.qty * (pos.entry_price - price))

    async def _open_upnl_total(self) -> float:
        total = 0.0
        for p in self._executor.open_positions.values():
            price = await self._router.price(p.symbol)
            if price:
                total += self._upnl(p, price)
        return total

    # ------------------------------------------------------------------ content
    async def status_text(self) -> str:
        st = await self._executor.portfolio_state()
        dd = (st.peak_equity - st.equity) / st.peak_equity if st.peak_equity > 0 else 0.0
        mode = "🧪 paper" if self._executor.is_paper else "🔴 LIVE"
        kill = "🛑 ВКЛ" if st.killed else "✅ выкл"
        q = self._s.base_quote
        upnl = await self._open_upnl_total()
        upnl_icon = "🟢" if upnl > 0 else ("🔴" if upnl < 0 else "⚪")
        # Overall closed-trade stats across all channels.
        rows = await self._scorer.all_stats()
        closed = sum(r.trades_closed for r in rows)
        wins = sum(r.wins for r in rows)
        cum = sum(r.cumulative_pnl for r in rows)
        wr = wins / closed if closed else 0.0
        dpnl = st.realized_pnl_today
        day_icon = "🟢" if dpnl > 0 else ("🔴" if dpnl < 0 else "⚪")
        return (
            f"<b>📊 ClonerBot — статус</b>  ({mode})\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Капитал: <b>{st.equity:,.2f}</b> {q}\n"
            f"⛰ Пик {st.peak_equity:,.2f} · Просадка {dd:.1%}\n"
            f"{day_icon} PnL сегодня: <b>{st.realized_pnl_today:+,.2f}</b> {q}\n"
            f"{upnl_icon} Плавающий PnL: <b>{upnl:+,.2f}</b> {q}\n"
            f"📌 Открыто позиций: <b>{st.open_count}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Всего сделок: <b>{closed}</b> · winrate <b>{wr:.0%}</b>\n"
            f"💵 Суммарный PnL: <b>{cum:+,.2f}</b> {q}\n"
            f"🚨 Аварийный стоп: {kill}"
        )

    async def positions_text(self) -> str:
        lines: list[str] = []
        # Real positions on the exchange (source of truth — includes any opened
        # outside the bot or before a restart).
        if self._router.has_exchanges:
            try:
                exps = await self._router.exchange_positions()
            except Exception as exc:
                exps = []
                log.warning("control.positions_failed", error=str(exc))
            if exps:
                lines.append("<b>📈 Позиции на бирже</b>")
                for p in exps:
                    d = "🟢 LONG" if p["side"] == "buy" else "🔴 SHORT"
                    lev = f" {p['leverage']:g}x" if p.get("leverage") else ""
                    lines.append(
                        f"• {_esc(p['exchange'])} <b>{_esc(p['symbol'])}</b> {d}{lev} — "
                        f"{p['qty']:g} @ {p['entry']:g} · PnL {p['pnl']:+,.2f}"
                    )
        # Positions the bot is actively managing — with LIVE metrics.
        bot = self._executor.open_positions
        if bot:
            lines.append("\n<b>🤖 Под управлением бота</b> (в реальном времени)")
            for p in bot.values():
                d = "🟢 LONG" if p.is_long else "🔴 SHORT"
                lev = f" {p.leverage:g}x" if p.leverage and p.leverage != 1 else ""
                price = await self._router.price(p.symbol)
                head = f"• <b>{_esc(p.symbol)}</b> {d}{lev} — {p.qty:g} @ {p.entry_price:g}"
                lines.append(head)
                if price:
                    upnl = self._upnl(p, price)
                    upct = upnl / p.cost * 100 if p.cost else 0.0
                    icon = "🟢" if upnl > 0 else ("🔴" if upnl < 0 else "⚪")
                    # Distance to stop and next take-profit (in %).
                    to_stop = ((price - p.stop_loss) / price if p.is_long
                               else (p.stop_loss - price) / price) * 100
                    nxt = (p.take_profits[p.tp_index]
                           if p.take_profits and p.tp_index < len(p.take_profits)
                           else p.take_profit)
                    tp_txt = ""
                    if nxt:
                        to_tp = (abs(nxt - price) / price) * 100
                        n = len(p.take_profits) or 1
                        tp_txt = f" · 🎯 до TP{p.tp_index + 1}/{n}: {to_tp:.2f}%"
                    lines.append(
                        f"   💹 {price:g} · {icon} PnL <b>{upnl:+,.2f}</b> ({upct:+.1f}%)\n"
                        f"   🛑 до стопа {to_stop:.2f}%{tp_txt} · 📡 {_esc(p.channel)}"
                    )
                else:
                    lines.append(f"   🛑 стоп {p.stop_loss:g} · 📡 {_esc(p.channel)}")
        return "\n".join(lines) if lines else "📈 Открытых позиций нет."

    async def history_text(self, limit: int = 15) -> str:
        async with session_scope() as s:
            rows = (
                await s.execute(
                    select(Position)
                    .where(Position.status == "closed")
                    .order_by(Position.closed_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
        if not rows:
            return "🧾 Закрытых сделок пока нет."
        lines = [f"<b>🧾 История сделок</b> (последние {len(rows)})"]
        total = 0.0
        for r in rows:
            pnl = r.realized_pnl or 0.0
            total += pnl
            icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
            tag = "🧪" if r.is_paper else "💵"
            reason = {"tp": "тейк", "sl": "стоп", "kill": "стоп-всё"}.get(r.close_reason or "", "—")
            lines.append(
                f"{icon} {tag} <b>{_esc(r.symbol)}</b> {pnl:+,.2f} "
                f"({r.entry_price:g}→{(r.exit_price or 0):g}, {reason}) · 📡 {_esc(r.channel)}"
            )
        lines.append(f"\nΣ по показанным: <b>{total:+,.2f}</b> {self._s.base_quote}")
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
                "📈 Позиции — открытые сделки в реальном времени (PnL, до стопа/тейка); "
                "кнопкой ❌ можно закрыть любую\n"
                "🧾 История сделок — последние закрытые сделки с PnL\n"
                "🏆 Рейтинг каналов — winrate и PnL по каналам\n"
                "➕ Добавить канал — подключить канал вручную (по @имени)\n"
                "📋 Кандидаты — одобрить/отклонить найденные каналы (✅/🚫)\n"
                "🔎 Искать каналы — запустить поиск сигнальных каналов\n"
                "🛑 Стоп-торговля — аварийно закрыть всё (с подтверждением)\n"
                "▶️ Возобновить — снять аварийный стоп\n"
                "💸 Вывод средств — ручной вывод (ввод командой)\n"
                "⚙️ Настройки — статус бирж и добавление API-ключей\n\n"
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

        async def _send_positions(target) -> None:  # noqa: ANN001
            text = await self.positions_text()
            symbols = list(self._executor.open_positions.keys())
            markup = kb.build_positions_kb(symbols) if symbols else None
            await target.answer(text, reply_markup=markup, parse_mode="HTML")

        @dp.message(F.text == kb.BTN_POSITIONS)
        async def _positions(message):  # noqa: ANN001
            if await deny(message):
                return
            await _send_positions(message)

        @dp.callback_query(F.data == kb.CB_REFRESH_POS)
        async def _positions_refresh(cb):  # noqa: ANN001
            if await deny(cb):
                return
            await _send_positions(cb.message)
            await cb.answer("Обновлено")

        @dp.callback_query(F.data.startswith(kb.CB_CLOSE))
        async def _close_one(cb):  # noqa: ANN001
            if await deny(cb):
                return
            symbol = cb.data[len(kb.CB_CLOSE):]
            if symbol not in self._executor.open_positions:
                await cb.answer("Позиция уже закрыта.", show_alert=True)
                return
            pnl = await self._executor.close_position(symbol, "manual")
            await cb.answer(f"Закрыто: {symbol}")
            await cb.message.answer(
                f"✅ Позиция <b>{_esc(symbol)}</b> закрыта вручную. "
                f"PnL: <b>{(pnl or 0):+,.2f}</b> {self._s.base_quote}", parse_mode="HTML")

        @dp.message(F.text == kb.BTN_HISTORY)
        async def _history(message):  # noqa: ANN001
            if await deny(message):
                return
            await message.answer(await self.history_text(), parse_mode="HTML")

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

        # ----- settings: exchanges -----
        @dp.message(F.text == kb.BTN_SETTINGS)
        async def _settings(message):  # noqa: ANN001
            if await deny(message):
                return
            await message.answer(
                f"⚙️ <b>Настройки</b>\nТекущий режим: "
                f"{'🔴 LIVE' if not self._executor.is_paper else '🧪 paper'}",
                reply_markup=kb.build_settings_kb(not self._executor.is_paper),
                parse_mode="HTML",
            )

        @dp.callback_query(F.data == kb.CB_EXCH_STATUS)
        async def _exch_status(cb):  # noqa: ANN001
            if await deny(cb):
                return
            await cb.answer("Проверяю…")
            await cb.message.answer(await self.exchange_status_text(), parse_mode="HTML")

        @dp.callback_query(F.data == kb.CB_EXCH_ADD)
        async def _exch_add(cb):  # noqa: ANN001
            if await deny(cb):
                return
            await cb.message.answer(
                "Выберите биржу для добавления:", reply_markup=kb.build_exchange_picker_kb()
            )
            await cb.answer()

        @dp.callback_query(F.data.startswith(kb.CB_ADDEX))
        async def _addex_pick(cb):  # noqa: ANN001
            if await deny(cb):
                return
            ex_id = cb.data[len(kb.CB_ADDEX):]
            self._pending[cb.from_user.id] = f"addex:{ex_id}"
            await cb.message.answer(
                f"🔑 Пришлите ключи для <b>{_esc(ex_id)}</b> одним сообщением:\n"
                f"<code>API_KEY SECRET</code>\n"
                f"(если биржа требует passphrase — третьим словом).\n\n"
                f"⚠️ Дайте ключу права только на <b>спот-торговлю</b>, вывод отключите. "
                f"После отправки удалите сообщение с ключами из чата.",
                parse_mode="HTML",
            )
            await cb.answer()

        @dp.callback_query(F.data == kb.CB_EXCH_DEL)
        async def _exch_del(cb):  # noqa: ANN001
            if await deny(cb):
                return
            ids = list(self._router.clients.keys())
            if not ids:
                await cb.answer("Нет подключённых бирж.", show_alert=True)
                return
            await cb.message.answer("Какую биржу удалить?",
                                    reply_markup=kb.build_delete_picker_kb(ids))
            await cb.answer()

        @dp.callback_query(F.data.startswith(kb.CB_DELEX))
        async def _delex(cb):  # noqa: ANN001
            if await deny(cb):
                return
            ex_id = cb.data[len(kb.CB_DELEX):]
            await cb.message.edit_text(await self.remove_exchange(ex_id), parse_mode="HTML")
            await cb.answer("Удалено")

        # ----- live/paper toggle -----
        @dp.callback_query(F.data == kb.CB_MODE_PAPER)
        async def _mode_paper(cb):  # noqa: ANN001
            if await deny(cb):
                return
            await cb.message.answer(await self.set_mode(live=False), parse_mode="HTML")
            await cb.answer()

        @dp.callback_query(F.data == kb.CB_MODE_LIVE)
        async def _mode_live(cb):  # noqa: ANN001
            if await deny(cb):
                return
            await cb.message.answer(
                "⚠️ <b>Переключить на LIVE?</b>\nСделки пойдут на реальные деньги "
                "по подключённым биржам.",
                reply_markup=kb.build_mode_confirm_kb(), parse_mode="HTML",
            )
            await cb.answer()

        @dp.callback_query(F.data == kb.CB_MODE_LIVE_YES)
        async def _mode_live_yes(cb):  # noqa: ANN001
            if await deny(cb):
                return
            await cb.message.edit_text(await self.set_mode(live=True), parse_mode="HTML")
            await cb.answer("LIVE включён")

        @dp.callback_query(F.data == kb.CB_MODE_NO)
        async def _mode_no(cb):  # noqa: ANN001
            if await deny(cb):
                return
            await cb.message.edit_text("Отменено. Режим не изменён.")
            await cb.answer()

        # ----- manual add channel -----
        @dp.message(F.text == kb.BTN_ADD_CHANNEL)
        async def _add_prompt(message):  # noqa: ANN001
            if await deny(message):
                return
            self._pending[message.from_user.id] = "add_channel"
            await message.answer(
                "➕ Отправьте <b>@имя_канала</b> (или ссылку t.me/имя), который нужно добавить.\n"
                "Он начнёт торговать на бумаге, а к деньгам допустится после проверки.",
                parse_mode="HTML",
            )

        @dp.message(Command("add"))
        async def _add_cmd(message):  # noqa: ANN001
            if await deny(message):
                return
            parts = (message.text or "").split()
            if len(parts) != 2:
                await message.answer("Формат: /add @channel")
                return
            await message.answer(await self.add_channel(parts[1]), parse_mode="HTML")

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
            wait = self._join_wait()
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

        # ----- free-text catch-all (registered last): captures pending input -----
        @dp.message(F.text)
        async def _free_text(message):  # noqa: ANN001
            if await deny(message):
                return
            action = self._pending.pop(message.from_user.id, None)
            if action == "add_channel":
                await message.answer(await self.add_channel(message.text), parse_mode="HTML")
            elif action and action.startswith("addex:"):
                ex_id = action.split(":", 1)[1]
                await message.answer(
                    await self.add_exchange(ex_id, message.text), parse_mode="HTML"
                )
            else:
                await message.answer("Не понял. Откройте меню: /start", reply_markup=menu)

        log.info("control.start", admins=list(self._admins))
        try:
            await dp.start_polling(bot, handle_signals=False)
        except asyncio.CancelledError:
            pass
        finally:
            await bot.session.close()
