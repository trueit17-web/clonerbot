"""Tests for channel discovery: store lifecycle, gate, promotion, shadow routing."""

from __future__ import annotations

from datetime import datetime, timezone

from clonerbot.config import Settings
from clonerbot.discovery import ACTIVE, DISCOVERED, OBSERVING, REJECTED
from clonerbot.discovery.gate import ChannelGate
from clonerbot.discovery.promotion import PromotionService
from clonerbot.discovery.store import CandidateStore
from clonerbot.execution.executor import Executor
from clonerbot.models.signal import RawMessage
from clonerbot.risk.risk_engine import RiskEngine
from clonerbot.scoring.channel_scorer import ChannelScorer


def _settings(**over) -> Settings:
    base = dict(
        _env_file=None, exchanges={}, mode="paper", paper_start_equity=10000.0,
        symbol_whitelist=["BTC", "ETH", "SOL"], tg_channels=["@manual"],
        promote_min_trades=3, promote_min_winrate=0.5, demote_winrate=0.3,
    )
    base.update(over)
    return Settings(**base)


# --------------------------------------------------------------------- store
async def test_store_lifecycle():
    store = CandidateStore()
    assert await store.upsert_discovered("@sigs", "Signals", 5000) is True
    # Duplicate upsert doesn't create a second row and keeps status.
    assert await store.upsert_discovered("@sigs", "Signals", 6000) is False
    cand = await store.get("@sigs")
    assert cand.status == DISCOVERED and cand.subscribers == 6000

    await store.approve("@sigs")
    assert (await store.get("@sigs")).status == OBSERVING
    assert (await store.get("@sigs")).joined is True

    await store.promote("@sigs")
    assert (await store.get("@sigs")).status == ACTIVE
    await store.demote("@sigs")
    assert (await store.get("@sigs")).status == OBSERVING
    await store.reject("@sigs")
    assert (await store.get("@sigs")).status == REJECTED


# ---------------------------------------------------------------------- gate
async def test_gate_manual_is_active():
    store = CandidateStore()
    gate = ChannelGate(_settings(), store)
    assert await gate.status("@manual") == ACTIVE
    assert await gate.is_ingesting("@manual") is True
    assert await gate.trades_real("@manual") is True


async def test_gate_discovered_flow():
    store = CandidateStore()
    gate = ChannelGate(_settings(), store)
    await store.upsert_discovered("@new", "New", 5000)
    # discovered → not ingested, not real
    assert await gate.is_ingesting("@new") is False
    assert await gate.trades_real("@new") is False
    await store.approve("@new")  # observing
    assert await gate.is_ingesting("@new") is True
    assert await gate.trades_real("@new") is False  # paper-only
    await store.promote("@new")  # active
    assert await gate.trades_real("@new") is True


async def test_gate_unknown_channel():
    gate = ChannelGate(_settings(), CandidateStore())
    assert await gate.status("@ghost") is None
    assert await gate.is_ingesting("@ghost") is False


# ----------------------------------------------------------------- promotion
async def test_promotion_promotes_after_paper_proof():
    store = CandidateStore()
    scorer = ChannelScorer()
    promo = PromotionService(_settings(), store)
    await store.upsert_discovered("@prov", "Proven", 5000)
    await store.approve("@prov")  # observing

    # 3 winning closes (>= promote_min_trades=3, winrate=1.0 >= 0.5, pnl>0).
    for _ in range(3):
        await scorer.record_close("@prov", pnl=10.0)
        await promo.on_channel_close("@prov")
    assert (await store.get("@prov")).status == ACTIVE


async def test_promotion_does_not_promote_losers():
    store = CandidateStore()
    scorer = ChannelScorer()
    promo = PromotionService(_settings(), store)
    await store.upsert_discovered("@bad", "Bad", 5000)
    await store.approve("@bad")
    for _ in range(3):
        await scorer.record_close("@bad", pnl=-5.0)  # all losses
        await promo.on_channel_close("@bad")
    assert (await store.get("@bad")).status == OBSERVING  # stays in paper


async def test_promotion_demotes_degraded_active():
    store = CandidateStore()
    scorer = ChannelScorer()
    promo = PromotionService(_settings(), store)
    await store.upsert_discovered("@deg", "Degraded", 5000)
    await store.approve("@deg")
    await store.promote("@deg")  # force ACTIVE
    # Feed mostly losses → winrate below demote_winrate=0.3 over >=3 trades.
    for pnl in (-5.0, -5.0, -5.0, 10.0):
        await scorer.record_close("@deg", pnl=pnl)
        await promo.on_channel_close("@deg")
    assert (await store.get("@deg")).status == OBSERVING


# --------------------------------------------------- pipeline shadow routing
async def test_pipeline_routes_observing_to_shadow(fake_router):
    from clonerbot.core.pipeline import Pipeline
    from clonerbot.parser.signal_parser import SignalParser

    settings = _settings()
    store = CandidateStore()
    gate = ChannelGate(settings, store)
    scorer = ChannelScorer()
    main = Executor(settings=settings, router=fake_router, scorer=scorer)
    shadow = Executor(settings=settings, router=fake_router, scorer=scorer, force_paper=True)
    risk = RiskEngine(settings, scorer)
    pipe = Pipeline(settings, SignalParser(use_llm=False), risk, main, scorer,
                    gate=gate, shadow_executor=shadow)

    await store.upsert_discovered("@obs", "Obs", 5000)
    await store.approve("@obs")  # observing → should go to shadow

    msg = RawMessage(channel="@obs", message_id=1,
                     text="BTC/USDT buy entry 60000 tp 66000 sl 58800",
                     posted_at=datetime.now(timezone.utc))
    await pipe.handle(msg)
    assert "BTC/USDT" in shadow.open_positions      # shadow took it
    assert "BTC/USDT" not in main.open_positions     # main did not

    # A manual (ACTIVE) channel goes to the main executor.
    msg2 = RawMessage(channel="@manual", message_id=2,
                      text="ETH/USDT buy entry 3000 tp 3300 sl 2900",
                      posted_at=datetime.now(timezone.utc))
    await pipe.handle(msg2)
    assert "ETH/USDT" in main.open_positions
