"""Anthropic LLM fallback for signals the regex layer can't confidently parse.

The LLM returns strict JSON matching the NormalizedSignal fields (or a null
signal when the message isn't a tradeable instruction — e.g. commentary, memes,
or a position update). We force tool-style JSON and validate it; anything that
doesn't validate goes to quarantine. The LLM is never allowed to invent prices —
it extracts only what the message states.
"""

from __future__ import annotations

import json

from clonerbot.config import get_settings
from clonerbot.logging_conf import get_logger

log = get_logger("llm_parser")

_SYSTEM = """You extract crypto trading signals from noisy Telegram messages.

Return ONLY the trade the message explicitly states. NEVER invent, guess, or \
extrapolate prices. If a field is not stated, leave it null/empty.

A message is NOT a signal (return "is_signal": false) when it is commentary, \
news, a meme, a question, an update about an already-open trade, an ad, or too \
ambiguous to trade safely.

Rules:
- side: "buy" for long/buy, "sell" for short/sell (spot: sell means avoid/close).
- base: the coin ticker, uppercase (e.g. BTC). quote: default USDT if unstated.
- entries: list of entry prices (a zone becomes two numbers). Empty = market.
- take_profits: list of target prices, ascending.
- stop_loss: single number or null.
- leverage: number if stated, else null (informational only).
- confidence: your 0..1 confidence this is a clean, tradeable signal."""

_TOOL = {
    "name": "emit_signal",
    "description": "Emit the structured trading signal extracted from the message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_signal": {"type": "boolean"},
            "base": {"type": ["string", "null"]},
            "quote": {"type": ["string", "null"]},
            "side": {"type": ["string", "null"], "enum": ["buy", "sell", None]},
            "entries": {"type": "array", "items": {"type": "number"}},
            "take_profits": {"type": "array", "items": {"type": "number"}},
            "stop_loss": {"type": ["number", "null"]},
            "leverage": {"type": ["number", "null"]},
            "confidence": {"type": "number"},
        },
        "required": ["is_signal", "confidence"],
    },
}


class LLMResult:
    def __init__(self, data: dict) -> None:
        self.is_signal: bool = bool(data.get("is_signal"))
        self.base: str | None = data.get("base")
        self.quote: str = (data.get("quote") or "USDT")
        self.side: str = data.get("side") or "buy"
        self.entries: list[float] = [float(x) for x in (data.get("entries") or [])]
        self.take_profits: list[float] = [float(x) for x in (data.get("take_profits") or [])]
        sl = data.get("stop_loss")
        self.stop_loss: float | None = float(sl) if sl is not None else None
        lev = data.get("leverage")
        self.leverage: float | None = float(lev) if lev is not None else None
        self.confidence: float = float(data.get("confidence", 0.0))


class LLMParser:
    def __init__(self) -> None:
        s = get_settings()
        self._model = s.anthropic_model
        self._api_key = s.anthropic_api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self._api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not configured")
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def parse(self, text: str) -> LLMResult | None:
        """Return an LLMResult, or None on error (caller quarantines on None)."""
        try:
            client = self._get_client()
            resp = await client.messages.create(
                model=self._model,
                max_tokens=512,
                system=_SYSTEM,
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "emit_signal"},
                messages=[{"role": "user", "content": text[:4000]}],
            )
        except Exception as exc:  # network/api errors → quarantine, don't crash
            log.warning("llm.error", error=str(exc))
            return None

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "emit_signal":
                data = block.input if isinstance(block.input, dict) else json.loads(block.input)
                return LLMResult(data)
        log.warning("llm.no_tool_use")
        return None
