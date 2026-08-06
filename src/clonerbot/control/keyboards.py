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
BTN_RATING = "🏆 Рейтинг каналов"
BTN_CANDIDATES = "📋 Кандидаты"
BTN_DISCOVER = "🔎 Искать каналы"
BTN_WITHDRAW = "💸 Вывод средств"
BTN_KILL = "🛑 Стоп-торговля"
BTN_RESUME = "▶️ Возобновить"
BTN_HELP = "ℹ️ Помощь"

# --- Inline callback-data prefixes ---
CB_APPROVE = "apr:"
CB_REJECT = "rej:"
CB_KILL_YES = "kill:yes"
CB_KILL_NO = "kill:no"


def build_main_menu(discovery_enabled: bool) -> ReplyKeyboardMarkup:
    """Persistent bottom menu, grouped by purpose. Discovery row shown only
    when discovery is enabled so the menu stays honest about what's available."""
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_POSITIONS)],
        [KeyboardButton(text=BTN_RATING)],
    ]
    if discovery_enabled:
        rows.append([KeyboardButton(text=BTN_DISCOVER), KeyboardButton(text=BTN_CANDIDATES)])
    rows.append([KeyboardButton(text=BTN_KILL), KeyboardButton(text=BTN_RESUME)])
    rows.append([KeyboardButton(text=BTN_WITHDRAW), KeyboardButton(text=BTN_HELP)])
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


def build_kill_confirm_kb() -> InlineKeyboardMarkup:
    """Confirmation for the emergency stop — a destructive action."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="⚠️ Да, остановить и закрыть всё", callback_data=CB_KILL_YES),
            InlineKeyboardButton(text="Отмена", callback_data=CB_KILL_NO),
        ]]
    )
