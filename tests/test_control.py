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


def test_settings_menu_present():
    labels = {b.text for row in kb.build_main_menu(False).keyboard for b in row}
    assert kb.BTN_SETTINGS in labels


def test_settings_kb_paper_offers_go_live():
    datas = [b.callback_data for row in kb.build_settings_kb(is_live=False).inline_keyboard
             for b in row]
    assert kb.CB_EXCH_STATUS in datas and kb.CB_EXCH_ADD in datas
    assert kb.CB_EXCH_DEL in datas and kb.CB_MODE_LIVE in datas


def test_settings_kb_live_offers_paper():
    datas = [b.callback_data for row in kb.build_settings_kb(is_live=True).inline_keyboard
             for b in row]
    assert kb.CB_MODE_PAPER in datas and kb.CB_MODE_LIVE not in datas


def test_exchange_picker_covers_known():
    datas = [b.callback_data for row in kb.build_exchange_picker_kb().inline_keyboard for b in row]
    assert f"{kb.CB_ADDEX}bybit" in datas
    assert len(datas) == len(kb.KNOWN_EXCHANGES)


def test_delete_picker_lists_exchanges():
    datas = [b.callback_data for row in kb.build_delete_picker_kb(["bybit", "okx"]).inline_keyboard
             for b in row]
    assert datas == [f"{kb.CB_DELEX}bybit", f"{kb.CB_DELEX}okx"]


def test_mode_confirm_kb():
    datas = [b.callback_data for row in kb.build_mode_confirm_kb().inline_keyboard for b in row]
    assert kb.CB_MODE_LIVE_YES in datas and kb.CB_MODE_NO in datas


# -------------------------------------------------------------- text builders
async def test_status_text_ru(fake_router):
    bot = _bot(fake_router)
    text = await bot.status_text()
    assert "Капитал" in text and "paper" in text


async def test_positions_text_empty(fake_router):
    bot = _bot(fake_router)
    assert "нет" in (await bot.positions_text()).lower()


async def test_positions_text_with_position(fake_router):
    bot = _bot(fake_router)
    plan = TradePlan(True, "ok", symbol="BTC/USDT", side=Side.buy, qty=0.01,
                     entry_price=60000.0, stop_loss=58800.0, take_profit=66000.0)
    await bot._executor.open_position(plan, channel="@vip", signal_id=None)
    text = await bot.positions_text()
    assert "BTC/USDT" in text and "@vip" in text


async def test_positions_text_shows_exchange_positions():
    # A position on the exchange the bot didn't open must still be shown.
    class _PosRouter(_FakeExRouter):
        async def exchange_positions(self):
            return [{"exchange": "bitunix", "symbol": "ETH/USDT", "side": "sell",
                     "qty": 0.5, "entry": 3000.0, "pnl": 12.5, "leverage": 5}]

    router = _PosRouter()
    router.clients["bitunix"] = object()  # make has_exchanges True
    bot = _bot_with(router)
    text = await bot.positions_text()
    assert "ETH/USDT" in text and "SHORT" in text and "бирже" in text


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


# --------------------------------------------------------------- exchanges UI
class _FakeExClient:
    def __init__(self, exchange, status):
        self.exchange_id = exchange
        self._status = status

    async def check(self, quote="USDT"):
        return self._status


class _FakeExRouter:
    def __init__(self, statuses=None):
        from clonerbot.exchange.ccxt_client import ExchangeStatus  # noqa: F401

        self.clients = {}
        self._statuses = statuses or {}

    @property
    def has_exchanges(self):
        return bool(self.clients)

    def add_client(self, exchange_id, creds):
        exchange_id = exchange_id.lower()
        self.clients[exchange_id] = _FakeExClient(
            exchange_id, self._statuses.get(exchange_id, self._default_ok(exchange_id))
        )

    @staticmethod
    def _default_ok(exchange_id):
        from clonerbot.exchange.ccxt_client import ExchangeStatus
        return ExchangeStatus(exchange_id, True, True, True, 1234.5, None)

    async def remove_client(self, exchange_id):
        return self.clients.pop(exchange_id.lower(), None) is not None

    async def status_all(self, quote="USDT"):
        return [c._status for c in self.clients.values()]


def _bot_with(router) -> ControlBot:
    s = _settings()
    return ControlBot(s, Executor(settings=s, router=router, scorer=ChannelScorer()),
                      ChannelScorer(), router)


async def test_exchange_status_text_none():
    bot = _bot_with(_FakeExRouter())
    assert "Ни одна биржа" in await bot.exchange_status_text()


async def test_exchange_status_text_authenticated():
    from clonerbot.exchange.ccxt_client import ExchangeStatus
    router = _FakeExRouter({"bybit": ExchangeStatus("bybit", True, True, True, 500.0, None)})
    router.add_client("bybit", {})
    bot = _bot_with(router)
    text = await bot.exchange_status_text()
    assert "bybit" in text and "ключи ок" in text and "500" in text


async def test_add_exchange_success():
    from clonerbot.exchange.ccxt_client import ExchangeStatus
    router = _FakeExRouter({"bybit": ExchangeStatus("bybit", True, True, True, 42.0, None)})
    bot = _bot_with(router)
    msg = await bot.add_exchange("bybit", "APIKEY123456 SECRET1234567")
    assert "подключена" in msg and "bybit" in msg
    assert "bybit" in router.clients


async def test_add_exchange_bad_keys_format():
    bot = _bot_with(_FakeExRouter())
    msg = await bot.add_exchange("bybit", "short")
    assert "не разобрал" in msg.lower()


async def test_add_exchange_auth_fails():
    from clonerbot.exchange.ccxt_client import ExchangeStatus
    router = _FakeExRouter({"bybit": ExchangeStatus("bybit", True, False, True, 0.0, "bad key")})
    bot = _bot_with(router)
    msg = await bot.add_exchange("bybit", "APIKEY123456 SECRET1234567")
    assert "не прошли проверку" in msg


async def test_status_text_shows_wallets():
    from clonerbot.exchange.ccxt_client import ExchangeStatus
    st = ExchangeStatus("bybit", True, True, True, 0.0, None, wallets="USDC: 500, ETH: 0.2")
    router = _FakeExRouter({"bybit": st})
    router.add_client("bybit", {})
    text = await _bot_with(router).exchange_status_text()
    assert "USDC: 500" in text  # funds surfaced even though USDT total is 0


async def test_status_text_tradable_line():
    from clonerbot.exchange.ccxt_client import ExchangeStatus
    st = ExchangeStatus("bybit", True, True, True, 300.0, None, wallets="USDT: 300", tradable=300.0)
    router = _FakeExRouter({"bybit": st})
    router.add_client("bybit", {})
    text = await _bot_with(router).exchange_status_text()
    assert "Доступно для торговли" in text and "300.00" in text


async def test_status_text_funds_not_on_spot_hint():
    from clonerbot.exchange.ccxt_client import ExchangeStatus
    # money exists (unified) but tradable spot balance is 0 → warn to move it
    st = ExchangeStatus("bybit", True, True, True, 500.0, None, wallets="USDT: 500", tradable=0.0)
    router = _FakeExRouter({"bybit": st})
    router.add_client("bybit", {})
    text = await _bot_with(router).exchange_status_text()
    assert "не на спотовом" in text


async def test_remove_exchange():
    router = _FakeExRouter()
    router.add_client("bybit", {})
    bot = _bot_with(router)
    msg = await bot.remove_exchange("bybit")
    assert "удалена" in msg and "bybit" not in router.clients


async def test_set_mode_toggles_and_persists(fake_router):
    from clonerbot.core.runtime import MODE_KEY, get_flag

    bot = _bot_with(fake_router)
    assert bot._executor.is_paper is True
    msg = await bot.set_mode(live=True)
    assert "LIVE" in msg
    assert bot._executor.is_paper is False           # shared settings flipped
    assert await get_flag(MODE_KEY) == "live"          # persisted
    await bot.set_mode(live=False)
    assert bot._executor.is_paper is True
    assert await get_flag(MODE_KEY) == "paper"
