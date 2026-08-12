"""Executor + pipeline tests in paper mode, fully offline via FakeRouter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from clonerbot.config import Settings
from clonerbot.execution.executor import Executor
from clonerbot.models.signal import RawMessage, Side
from clonerbot.risk.risk_engine import RiskEngine, TradePlan
from clonerbot.scoring.channel_scorer import ChannelScorer


def _settings(**over) -> Settings:
    base = dict(_env_file=None, exchanges={}, mode="paper", paper_start_equity=10000.0,
                symbol_whitelist=["BTC", "ETH", "SOL"])
    base.update(over)
    return Settings(**base)


@pytest.fixture
def executor(fake_router):
    return Executor(settings=_settings(), router=fake_router, scorer=ChannelScorer())


def _plan(symbol="BTC/USDT", qty=0.01, entry=60000.0, sl=58800.0, tp=66000.0) -> TradePlan:
    return TradePlan(True, "ok", symbol=symbol, side=Side.buy, qty=qty,
                     entry_price=entry, stop_loss=sl, take_profit=tp)


async def test_open_reduces_cash_and_tracks_position(executor):
    pos = await executor.open_position(_plan(), channel="@vip", signal_id=None)
    assert pos is not None
    assert "BTC/USDT" in executor.open_positions
    # cash spent ~ qty*price + fee
    assert executor.paper.cash < 10000.0


async def test_take_profit_closes_with_profit(executor, fake_router):
    await executor.open_position(_plan(), channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 66000.0)  # TP hit
    await executor._check_positions()
    assert "BTC/USDT" not in executor.open_positions
    # realized PnL should be positive
    assert executor._realized_today > 0


async def test_stop_loss_closes_with_loss(executor, fake_router):
    await executor.open_position(_plan(), channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 58000.0)  # below SL
    await executor._check_positions()
    assert "BTC/USDT" not in executor.open_positions
    assert executor._realized_today < 0


async def test_no_duplicate_position(executor):
    await executor.open_position(_plan(), channel="@vip", signal_id=None)
    dup = await executor.open_position(_plan(), channel="@vip", signal_id=None)
    assert dup is None
    assert len(executor.open_positions) == 1


async def test_kill_closes_all(executor):
    await executor.open_position(_plan(), channel="@vip", signal_id=None)
    await executor.open_position(_plan(symbol="ETH/USDT", entry=3000, sl=2900, tp=3300),
                                 channel="@vip", signal_id=None)
    executor.killed = True
    n = await executor.close_all("kill")
    assert n == 2 and not executor.open_positions


async def test_monitor_loop_survives_timeout_and_stops(fake_router):
    # Regression: on Python 3.10 asyncio.wait_for raises asyncio.TimeoutError,
    # which is NOT the builtin TimeoutError. If the interval-elapsed path were
    # caught with the wrong type, the monitor task would crash after one tick
    # and take the whole app down. interval=0 forces the timeout path every
    # iteration; the loop must keep running until `stop` is set.
    ex = Executor(settings=_settings(monitor_interval_sec=0), router=fake_router,
                  scorer=ChannelScorer())
    stop = asyncio.Event()

    async def _stopper():
        await asyncio.sleep(0.1)
        stop.set()

    # wait_for guards against a hang; a crash would propagate and fail the test.
    await asyncio.wait_for(asyncio.gather(ex.monitor_loop(stop), _stopper()), timeout=5)
    assert stop.is_set()


async def test_notifier_fires_on_open_and_close(fake_router):
    events: list[str] = []

    async def notify(text):
        events.append(text)

    ex = Executor(settings=_settings(), router=fake_router, scorer=ChannelScorer(),
                  notifier=notify)
    await ex.open_position(_plan(), channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 66000.0)  # TP
    await ex._check_positions()
    assert any("Открыта" in e and "@vip" in e for e in events)
    assert any("Закрыта" in e and "тейк" in e for e in events)


async def test_scale_out_across_three_tps(fake_router):
    # 3 TP levels → partial closes at TP1 & TP2, full remainder at TP3.
    ex = Executor(settings=_settings(paper_slippage=0.0), router=fake_router,
                  scorer=ChannelScorer())
    plan = TradePlan(True, "ok", symbol="BTC/USDT", side=Side.buy, qty=0.03,
                     entry_price=60000.0, stop_loss=58800.0, take_profit=66000.0,
                     take_profits=[66000.0, 67000.0, 68000.0])
    pos = await ex.open_position(plan, channel="@vip", signal_id=None)
    assert pos.orig_qty == 0.03

    fake_router.set_price("BTC/USDT", 66000.0)     # TP1 → close ~1/3
    await ex._check_positions()
    assert "BTC/USDT" in ex.open_positions
    assert ex.open_positions["BTC/USDT"].qty == pytest.approx(0.02, rel=1e-6)
    # stop moved to breakeven after TP1
    assert ex.open_positions["BTC/USDT"].stop_loss == pytest.approx(60000.0)

    fake_router.set_price("BTC/USDT", 67000.0)     # TP2 → close another ~1/3
    await ex._check_positions()
    assert ex.open_positions["BTC/USDT"].qty == pytest.approx(0.01, rel=1e-6)

    fake_router.set_price("BTC/USDT", 68000.0)     # TP3 → close the rest
    await ex._check_positions()
    assert "BTC/USDT" not in ex.open_positions
    assert ex._realized_today > 0


async def test_scale_out_stop_after_partial_banks_pnl(fake_router):
    # After TP1 partial, a stop-out on the remainder still nets positive overall.
    import pytest as _pytest  # noqa

    ex = Executor(settings=_settings(move_stop_to_breakeven=False),
                  router=fake_router, scorer=ChannelScorer())
    plan = TradePlan(True, "ok", symbol="BTC/USDT", side=Side.buy, qty=0.03,
                     entry_price=60000.0, stop_loss=58800.0, take_profit=66000.0,
                     take_profits=[66000.0, 70000.0, 74000.0])
    await ex.open_position(plan, channel="@vip", signal_id=None)
    fake_router.set_price("BTC/USDT", 66000.0)     # TP1 banks profit on 1/3
    await ex._check_positions()
    banked = ex.open_positions["BTC/USDT"].realized_accum
    assert banked > 0
    fake_router.set_price("BTC/USDT", 58000.0)     # stop the remainder
    await ex._check_positions()
    assert "BTC/USDT" not in ex.open_positions


async def test_max_hold_time_exit(fake_router):
    from datetime import timedelta

    ex = Executor(settings=_settings(max_hold_minutes=1), router=fake_router,
                  scorer=ChannelScorer())
    await ex.open_position(_plan(), channel="@vip", signal_id=None)
    pos = ex.open_positions["BTC/USDT"]
    pos.opened_at = datetime.now(timezone.utc) - timedelta(minutes=2)  # aged out
    fake_router.set_price("BTC/USDT", 61000.0)  # between SL and TP (no SL/TP trigger)
    await ex._check_positions()
    assert "BTC/USDT" not in ex.open_positions  # closed by max-hold


async def test_recover_open_positions(fake_router):
    ex1 = Executor(settings=_settings(), router=fake_router, scorer=ChannelScorer())
    await ex1.open_position(_plan(), channel="@vip", signal_id=None)
    # New executor instance (simulating restart) recovers from DB.
    ex2 = Executor(settings=_settings(), router=fake_router, scorer=ChannelScorer())
    await ex2.recover_open_positions()
    assert "BTC/USDT" in ex2.open_positions


async def test_pipeline_end_to_end(fake_router):
    from clonerbot.core.pipeline import Pipeline
    from clonerbot.parser.signal_parser import SignalParser

    settings = _settings()
    scorer = ChannelScorer()
    executor = Executor(settings=settings, router=fake_router, scorer=scorer)
    risk = RiskEngine(settings, scorer)
    parser = SignalParser(use_llm=False)
    pipe = Pipeline(settings, parser, risk, executor, scorer)

    msg = RawMessage(
        channel="@vip", message_id=42,
        text="BTC/USDT buy entry 60000 tp 66000 sl 58800",
        posted_at=datetime.now(timezone.utc),
    )
    await pipe.handle(msg)
    assert "BTC/USDT" in executor.open_positions

    # A quarantined (non-signal) message must NOT open anything.
    msg2 = RawMessage(channel="@vip", message_id=43, text="gm friends bullish vibes",
                      posted_at=datetime.now(timezone.utc))
    await pipe.handle(msg2)
    assert len(executor.open_positions) == 1
