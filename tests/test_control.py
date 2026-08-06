"""Tests for the button-based control bot: keyboard layout and text builders.

The aiogram polling loop isn't exercised here (needs a live token); we test the
pure pieces — keyboard construction and the message-text builders.
"""

from __future__ import annotations

from clonerbot.config import Settings
from clonerbot.control import keyboards as kb
from clonerbot.control.telegram_bot import ControlBot
from clonerbot.execution.executor import Executor
from clonerbot.models.signal import Side
from clonerbot.risk.risk_engine import TradePlan
from clonerbot.scoring.channel_scorer import ChannelScorer


def _settings(**over) -> Settings:
    base = dict(_env_file=None, exchanges={}, mode="paper", paper_start_equity=10000.0,
                symbol_whitelist=["BTC"])
    base.update(over)
    return Settings(**base)


def _bot(router) -> ControlBot:
    s = _settings()
    return ControlBot(s, Executor(settings=s, router=router, scorer=ChannelScorer()),
                      ChannelScorer(), router)


# ------------------------------------------------------------------ keyboards
def test_main_menu_hides_discovery_when_disabled():
    menu = kb.build_main_menu(discovery_enabled=False)
    labels = {b.text for row in menu.keyboard for b in row}
    assert kb.BTN_STATUS in labels and kb.BTN_KILL in labels
    assert kb.BTN_DISCOVER not in labels and kb.BTN_CANDIDATES not in labels


def test_main_menu_shows_discovery_when_enabled():
    menu = kb.build_main_menu(discovery_enabled=True)
    labels = {b.text for row in menu.keyboard for b in row}
    assert kb.BTN_DISCOVER in labels and kb.BTN_CANDIDATES in labels


def test_candidate_kb_encodes_channel():
    m = kb.build_candidate_kb("@somechan")
    datas = [b.callback_data for row in m.inline_keyboard for b in row]
    assert f"{kb.CB_APPROVE}@somechan" in datas
    assert f"{kb.CB_REJECT}@somechan" in datas


def test_kill_confirm_kb():
    m = kb.build_kill_confirm_kb()
    datas = [b.callback_data for row in m.inline_keyboard for b in row]
    assert kb.CB_KILL_YES in datas and kb.CB_KILL_NO in datas


# -------------------------------------------------------------- text builders
async def test_status_text_ru(fake_router):
    bot = _bot(fake_router)
    text = await bot.status_text()
    assert "Капитал" in text and "paper" in text


async def test_positions_text_empty(fake_router):
    bot = _bot(fake_router)
    assert "нет" in (bot.positions_text()).lower()


async def test_positions_text_with_position(fake_router):
    bot = _bot(fake_router)
    plan = TradePlan(True, "ok", symbol="BTC/USDT", side=Side.buy, qty=0.01,
                     entry_price=60000.0, stop_loss=58800.0, take_profit=66000.0)
    await bot._executor.open_position(plan, channel="@vip", signal_id=None)
    text = bot.positions_text()
    assert "BTC/USDT" in text and "@vip" in text


async def test_rating_text_empty(fake_router):
    bot = _bot(fake_router)
    assert "пока нет" in await bot.rating_text()
