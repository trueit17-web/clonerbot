"""The normalized signal — the single contract every downstream component speaks.

The Telegram parser (regex or LLM) must produce this shape or reject the message
to quarantine. Keeping the parser fully decoupled behind this schema means we can
add new signal sources later without touching the risk or execution engines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Side(str, Enum):
    buy = "buy"
    sell = "sell"


class ParseMethod(str, Enum):
    regex = "regex"
    llm = "llm"


class RawMessage(BaseModel):
    """A raw Telegram message before parsing."""

    channel: str
    message_id: int
    text: str
    posted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def dedup_key(self) -> str:
        return f"{self.channel}:{self.message_id}"


class NormalizedSignal(BaseModel):
    """A parsed, structured trade instruction.

    For MVP (spot), `side=buy` opens a position and `sell` closes/avoids it.
    Prices are in quote currency. All price levels are optional except that a
    signal with neither entry nor a market intent is rejected upstream.
    """

    # Provenance
    channel: str
    message_id: int
    posted_at: datetime
    parse_method: ParseMethod
    parse_confidence: float = Field(ge=0.0, le=1.0, default=1.0)

    # Trade content
    base: str  # e.g. "BTC"
    quote: str = "USDT"
    side: Side = Side.buy
    entries: list[float] = Field(default_factory=list)  # entry zone, may be empty (=market)
    take_profits: list[float] = Field(default_factory=list)
    stop_loss: float | None = None
    # Leverage is captured for provenance/logging but IGNORED in spot MVP.
    leverage: float | None = None

    @field_validator("base", "quote")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("entries", "take_profits")
    @classmethod
    def _positive_sorted(cls, v: list[float]) -> list[float]:
        return sorted(p for p in v if p and p > 0)

    @property
    def symbol(self) -> str:
        """CCXT unified symbol, e.g. 'BTC/USDT'."""
        return f"{self.base}/{self.quote}"

    @property
    def is_long(self) -> bool:
        """Futures direction: buy = long, sell = short."""
        return self.side is Side.buy

    @property
    def dedup_key(self) -> str:
        return f"{self.channel}:{self.message_id}"

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.posted_at).total_seconds()

    def reference_entry(self) -> float | None:
        """Best single entry price to reason about (midpoint of the entry zone)."""
        if not self.entries:
            return None
        return sum(self.entries) / len(self.entries)
