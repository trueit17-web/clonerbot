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


def test_tp_index_not_mistaken_for_value():
    # "Take Profit 1 (TP1): 4370" must read 4370, not the ordinal 1.
    r = parse_regex("BTC LONG\nEntry: 4300\nTake Profit 1 (TP1): 4370\nStop Loss: 4200")
    assert r is not None
    assert r.base == "BTC" and r.side == "buy"
    assert r.entries == [4300.0]
    assert r.take_profits == [4370.0]   # not [1.0]
    assert r.stop_loss == 4200.0


def test_bare_ticker_next_to_direction():
    r = parse_regex("ETH SHORT entry 3000 TP1: 2900 SL: 3100")
    assert r is not None and r.base == "ETH" and r.side == "sell"
    assert r.take_profits == [2900.0] and r.stop_loss == 3100.0


def test_direction_word_not_taken_as_ticker():
    r = parse_regex("buy BTC entry 4300 tp 4370 sl 4200")
    assert r is not None and r.base == "BTC"  # not "BUY"


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
