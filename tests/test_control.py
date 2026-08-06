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
def test_main_menu_hides_only_search_when_disabled():
    menu = kb.build_main_menu(discovery_enabled=False)
    labels = {b.text for row in menu.keyboard for b in row}
    # Core + always-on trust actions present; only the auto-search button hidden.
    assert kb.BTN_STATUS in labels and kb.BTN_KILL in labels
    assert kb.BTN_HISTORY in labels and kb.BTN_ADD_CHANNEL in labels
    assert kb.BTN_CANDIDATES in labels
    assert kb.BTN_DISCOVER not in labels


def test_main_menu_shows_search_when_enabled():
    menu = kb.build_main_menu(discovery_enabled=True)
    labels = {b.text for row in menu.keyboard for b in row}
    assert kb.BTN_DISCOVER in labels


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


# ------------------------------------------------------------------ history
async def test_history_text_empty(fake_router):
    bot = _bot(fake_router)
    assert "пока нет" in await bot.history_text()


async def test_history_text_lists_closed_trades(fake_router):
    bot = _bot(fake_router)
    plan = TradePlan(True, "ok", symbol="BTC/USDT", side=Side.buy, qty=0.01,
                     entry_price=60000.0, stop_loss=58800.0, take_profit=66000.0)
    await bot._executor.open_position(plan, channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 66000.0)
    await bot._executor._check_positions()  # closes at TP
    text = await bot.history_text()
    assert "BTC/USDT" in text and "тейк" in text and "@vip" in text


# ---------------------------------------------------------------- add channel
class _FakeListener:
    def __init__(self, ok=True):
        self.ok = ok
        self.joined = []

    async def join_channel(self, channel):
        if not self.ok:
            raise RuntimeError("cannot join")
        self.joined.append(channel)
        return f"Title of {channel}"


async def test_add_channel_joins_and_observes(fake_router):
    from clonerbot.discovery import OBSERVING
    from clonerbot.discovery.store import CandidateStore

    s = _settings()
    store = CandidateStore()
    listener = _FakeListener(ok=True)
    bot = ControlBot(s, Executor(settings=s, router=fake_router, scorer=ChannelScorer()),
                     ChannelScorer(), fake_router, store=store, listener=listener)
    msg = await bot.add_channel("newsignals")  # no @ prefix on purpose
    assert "newsignals" in msg and listener.joined == ["@newsignals"]
    cand = await store.get("@newsignals")
    assert cand is not None and cand.status == OBSERVING


async def test_add_channel_join_failure(fake_router):
    s = _settings()
    bot = ControlBot(s, Executor(settings=s, router=fake_router, scorer=ChannelScorer()),
                     ChannelScorer(), fake_router, listener=_FakeListener(ok=False))
    msg = await bot.add_channel("@bad")
    assert "не удалось" in msg.lower()


async def test_add_channel_cooldown(fake_router):
    import time as _t

    s = _settings(join_cooldown_sec=9999)
    bot = ControlBot(s, Executor(settings=s, router=fake_router, scorer=ChannelScorer()),
                     ChannelScorer(), fake_router, listener=_FakeListener(ok=True))
    bot._last_join = _t.monotonic()  # just joined
    msg = await bot.add_channel("@channel1")
    assert "пауза" in msg.lower()
