"""Top-level signal parser: regex first, LLM fallback, quarantine on failure.

Flow for each RawMessage:
  1. Try the deterministic regex parser. If confident, use it (no LLM cost).
  2. Otherwise ask the Anthropic LLM. If it says "not a signal" or fails
     validation, quarantine.
  3. Build a NormalizedSignal and let pydantic validate it. Any validation
     error → quarantine.

`quarantined` results are recorded for audit but never traded — the guiding
principle is that an unclear signal must be dropped, not guessed.
"""

from __future__ import annotations

from dataclasses import dataclass

from clonerbot.logging_conf import get_logger
from clonerbot.models.signal import NormalizedSignal, ParseMethod, RawMessage, Side
from clonerbot.parser.llm_parser import LLMParser
from clonerbot.parser.regex_rules import parse_regex

log = get_logger("parser")

# Regex confidence at or above this is trusted without calling the LLM.
_REGEX_TRUST = 0.7


@dataclass
class ParseOutcome:
    signal: NormalizedSignal | None
    quarantined: bool
    reason: str


class SignalParser:
    def __init__(self, llm: LLMParser | None = None, use_llm: bool = True) -> None:
        self._llm = llm or LLMParser()
        self._use_llm = use_llm

    async def parse(self, msg: RawMessage) -> ParseOutcome:
        text = (msg.text or "").strip()
        if not text:
            return ParseOutcome(None, True, "empty message")

        # 1) Regex path
        rr = parse_regex(text)
        if rr and rr.confidence >= _REGEX_TRUST:
            return self._build(
                msg, ParseMethod.regex, rr.confidence,
                rr.base, rr.quote, rr.side, rr.entries, rr.take_profits,
                rr.stop_loss, rr.leverage,
            )

        # 2) LLM fallback
        if not self._use_llm:
            return ParseOutcome(None, True, "low-confidence regex, LLM disabled")

        lr = await self._llm.parse(text)
        if lr is None:
            return ParseOutcome(None, True, "LLM error or no structured output")
        if not lr.is_signal:
            return ParseOutcome(None, True, "LLM: not a tradeable signal")
        if not lr.base:
            return ParseOutcome(None, True, "LLM: missing symbol")

        return self._build(
            msg, ParseMethod.llm, lr.confidence,
            lr.base, lr.quote, lr.side, lr.entries, lr.take_profits,
            lr.stop_loss, lr.leverage,
        )

    def _build(
        self,
        msg: RawMessage,
        method: ParseMethod,
        confidence: float,
        base: str,
        quote: str,
        side: str,
        entries: list[float],
        take_profits: list[float],
        stop_loss: float | None,
        leverage: float | None,
    ) -> ParseOutcome:
        try:
            signal = NormalizedSignal(
                channel=msg.channel,
                message_id=msg.message_id,
                posted_at=msg.posted_at,
                parse_method=method,
                parse_confidence=max(0.0, min(1.0, confidence)),
                base=base,
                quote=quote or "USDT",
                side=Side(side),
                entries=entries,
                take_profits=take_profits,
                stop_loss=stop_loss,
                leverage=leverage,
            )
        except Exception as exc:
            log.warning("parse.validation_failed", error=str(exc), key=msg.dedup_key)
            return ParseOutcome(None, True, f"validation failed: {exc}")

        log.info(
            "parse.ok",
            key=msg.dedup_key,
            method=method.value,
            symbol=signal.symbol,
            side=signal.side.value,
            confidence=signal.parse_confidence,
        )
        return ParseOutcome(signal, False, "parsed")
