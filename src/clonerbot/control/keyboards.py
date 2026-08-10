"""Keyboards and button labels for the Russian control bot.

Kept separate from the bot wiring so the layout can be unit-tested without a
running Telegram connection. All user-facing text is Russian.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# --- Main-menu button labels (also matched by the message handlers) ---
BTN_STATUS = "📊 Статус"
BTN_POSITIONS = "📈 Позиции"
BTN_HISTORY = "🧾 История сделок"
BTN_RATING = "🏆 Рейтинг каналов"
BTN_ADD_CHANNEL = "➕ Добавить канал"
BTN_CANDIDATES = "📋 Кандидаты"
BTN_DISCOVER = "🔎 Искать каналы"
BTN_WITHDRAW = "💸 Вывод средств"
BTN_SETTINGS = "⚙️ Настройки"
BTN_KILL = "🛑 Стоп-торговля"
BTN_RESUME = "▶️ Возобновить"
BTN_HELP = "ℹ️ Помощь"

# --- Inline callback-data prefixes ---
CB_APPROVE = "apr:"
CB_REJECT = "rej:"
CB_KILL_YES = "kill:yes"
CB_KILL_NO = "kill:no"
CB_EXCH_STATUS = "exch:status"
CB_EXCH_ADD = "exch:add"
CB_EXCH_DEL = "exch:del"
CB_ADDEX = "addex:"    # + ccxt exchange id
CB_DELEX = "delex:"    # + ccxt exchange id
CB_MODE_LIVE = "mode:live"
CB_MODE_PAPER = "mode:paper"
CB_MODE_LIVE_YES = "mode:live:yes"
CB_MODE_NO = "mode:no"

# Common spot exchanges offered when adding one via the bot.
KNOWN_EXCHANGES = ["bybit", "binance", "bitunix", "okx", "bitget", "kucoin", "gate", "mexc"]


def build_main_menu(discovery_enabled: bool) -> ReplyKeyboardMarkup:
    """Persistent bottom menu, grouped by purpose. Discovery row shown only
    when discovery is enabled so the menu stays honest about what's available."""
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_POSITIONS)],
        [KeyboardButton(text=BTN_HISTORY), KeyboardButton(text=BTN_RATING)],
        # Manual add + candidate review are always available (trust machinery is
        # always on); the periodic auto-search button appears only when enabled.
        [KeyboardButton(text=BTN_ADD_CHANNEL), KeyboardButton(text=BTN_CANDIDATES)],
    ]
    if discovery_enabled:
        rows.append([KeyboardButton(text=BTN_DISCOVER)])
    rows.append([KeyboardButton(text=BTN_KILL), KeyboardButton(text=BTN_RESUME)])
    rows.append([KeyboardButton(text=BTN_WITHDRAW), KeyboardButton(text=BTN_SETTINGS)])
    rows.append([KeyboardButton(text=BTN_HELP)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие…",
    )


def build_candidate_kb(channel: str) -> InlineKeyboardMarkup:
    """Approve / reject buttons attached to one candidate message."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"{CB_APPROVE}{channel}"),
            InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"{CB_REJECT}{channel}"),
        ]]
    )


def build_settings_kb(is_live: bool) -> InlineKeyboardMarkup:
    """Settings submenu: exchange status, add/remove exchange, mode toggle."""
    mode_btn = (
        InlineKeyboardButton(text="🧪 Вернуть paper", callback_data=CB_MODE_PAPER)
        if is_live else
        InlineKeyboardButton(text="🔴 Включить LIVE", callback_data=CB_MODE_LIVE)
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔌 Статус бирж", callback_data=CB_EXCH_STATUS)],
            [InlineKeyboardButton(text="➕ Добавить биржу", callback_data=CB_EXCH_ADD)],
            [InlineKeyboardButton(text="🗑 Удалить биржу", callback_data=CB_EXCH_DEL)],
            [mode_btn],
        ]
    )


def build_delete_picker_kb(exchange_ids: list[str]) -> InlineKeyboardMarkup:
    """One delete button per currently-connected exchange."""
    rows = [
        [InlineKeyboardButton(text=f"🗑 {ex}", callback_data=f"{CB_DELEX}{ex}")]
        for ex in exchange_ids
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_mode_confirm_kb() -> InlineKeyboardMarkup:
    """Confirmation before switching to LIVE (real money)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="⚠️ Да, включить LIVE", callback_data=CB_MODE_LIVE_YES),
            InlineKeyboardButton(text="Отмена", callback_data=CB_MODE_NO),
        ]]
    )


def build_exchange_picker_kb() -> InlineKeyboardMarkup:
    """Grid of known exchanges to add (two per row)."""
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(KNOWN_EXCHANGES), 2):
        rows.append([
            InlineKeyboardButton(text=ex.capitalize(), callback_data=f"{CB_ADDEX}{ex}")
            for ex in KNOWN_EXCHANGES[i:i + 2]
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_kill_confirm_kb() -> InlineKeyboardMarkup:
    """Confirmation for the emergency stop — a destructive action."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="⚠️ Да, остановить и закрыть всё", callback_data=CB_KILL_YES),
            InlineKeyboardButton(text="Отмена", callback_data=CB_KILL_NO),
        ]]
    )
