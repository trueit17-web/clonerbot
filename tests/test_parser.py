"""Parser tests — regex path only (no LLM/network)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from clonerbot.models.signal import RawMessage
from clonerbot.parser.regex_rules import parse_regex
from clonerbot.parser.signal_parser import SignalParser


def _raw(text: str) -> RawMessage:
    return RawMessage(
        channel="@test", message_id=1, text=text, posted_at=datetime.now(timezone.utc)
    )


def test_regex_full_signal():
    r = parse_regex("BTC/USDT LONG\nEntry: 62000 - 61500\nTargets: 64000, 66000\nStop: 60000")
    assert r is not None
    assert r.base == "BTC" and r.quote == "USDT"
    assert r.side == "buy"
    # parse_regex returns raw captured order (sorting happens in NormalizedSignal).
    assert sorted(r.entries) == [61500.0, 62000.0]
    assert r.take_profits == [64000.0, 66000.0]
    assert r.stop_loss == 60000.0
    assert r.confidence >= 0.7


def test_regex_short():
    r = parse_regex("SHORT SOL/USDT entry 150 targets 140 130 stop 160")
    assert r is not None and r.side == "sell"


def test_regex_rejects_commentary():
    assert parse_regex("gm, market looking bullish, thoughts?") is None


async def test_parser_quarantines_when_llm_disabled():
    parser = SignalParser(use_llm=False)
    # A message the regex can't confidently parse must be quarantined, not guessed.
    outcome = await parser.parse(_raw("thinking about eth here, maybe soon"))
    assert outcome.signal is None
    assert outcome.quarantined


async def test_parser_accepts_clean_signal():
    parser = SignalParser(use_llm=False)
    outcome = await parser.parse(_raw("BTC/USDT buy entry 60000 tp 66000 sl 58800"))
    assert outcome.signal is not None
    assert outcome.signal.symbol == "BTC/USDT"
    assert outcome.signal.stop_loss == 58800.0


@pytest.mark.parametrize("text", ["", "   ", "\n"])
async def test_parser_empty(text):
    parser = SignalParser(use_llm=False)
    outcome = await parser.parse(_raw(text))
    assert outcome.quarantined
