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
CB_ADDEX = "addex:"  # + ccxt exchange id

# Common spot exchanges offered when adding one via the bot.
KNOWN_EXCHANGES = ["bybit", "binance", "okx", "bitget", "kucoin", "gate", "mexc", "kraken"]


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


def build_settings_kb() -> InlineKeyboardMarkup:
    """Settings submenu: exchange connection status and adding a new exchange."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔌 Статус бирж", callback_data=CB_EXCH_STATUS)],
            [InlineKeyboardButton(text="➕ Добавить биржу", callback_data=CB_EXCH_ADD)],
        ]
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
