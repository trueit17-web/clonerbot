"""Fast, deterministic regex parsing for common Telegram signal formats.

This handles the ~majority of well-formatted signals cheaply and without an LLM
call. Anything it can't confidently parse is handed to the LLM fallback, and
whatever the LLM also can't parse goes to quarantine (never traded).

Typical formats handled, e.g.:
    BTC/USDT LONG
    Entry: 62000 - 61500
    Targets: 64000, 66000, 68000
    Stop: 60000

    #ETH buy zone 3000-2950 tp 3200 sl 2900

We deliberately keep this conservative: it is better to fall through to the LLM
than to misread a signal and trade on a wrong number.
"""

from __future__ import annotations

import re

# Words that indicate direction.
_LONG_WORDS = re.compile(r"\b(long|buy|лонг|покупк\w*|бай)\b", re.IGNORECASE)
_SHORT_WORDS = re.compile(r"\b(short|sell|шорт|продаж\w*|селл)\b", re.IGNORECASE)

# A trading pair like BTC/USDT, BTCUSDT, $BTC, #ETH, or a bare majors ticker.
_PAIR = re.compile(
    r"(?:[#$])?\b(?P<base>[A-Z]{2,10})\s*[/\-]?\s*(?P<quote>USDT|USDC|USD|BUSD|BTC|ETH)\b",
    re.IGNORECASE,
)
_BARE_TICKER = re.compile(r"(?:[#$])(?P<base>[A-Za-z]{2,10})\b")

_NUM = r"(\d+(?:[.,]\d+)?)"

_ENTRY = re.compile(
    rf"(?:entry|enter|buy\s*zone|zone|вход|цена)\D{{0,12}}?{_NUM}(?:\s*[-–—to]+\s*{_NUM})?",
    re.IGNORECASE,
)
_TP = re.compile(
    rf"(?:tp\d*|target\w*|take[\s-]?profit|тейк|цел\w*)\D{{0,12}}?((?:{_NUM}[,\s/]*)+)",
    re.IGNORECASE,
)
_SL = re.compile(
    rf"(?:sl|stop[\s-]?loss|stop|стоп)\D{{0,12}}?{_NUM}",
    re.IGNORECASE,
)
_LEV = re.compile(rf"(?:lev\w*|leverage|плечо|x)\s*[:=]?\s*{_NUM}\s*x?", re.IGNORECASE)
_NUM_ONLY = re.compile(_NUM)


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


class RegexParseResult:
    __slots__ = (
        "base",
        "quote",
        "side",
        "entries",
        "take_profits",
        "stop_loss",
        "leverage",
        "confidence",
    )

    def __init__(self) -> None:
        self.base: str | None = None
        self.quote: str = "USDT"
        self.side: str = "buy"
        self.entries: list[float] = []
        self.take_profits: list[float] = []
        self.stop_loss: float | None = None
        self.leverage: float | None = None
        self.confidence: float = 0.0


def parse_regex(text: str) -> RegexParseResult | None:
    """Attempt a deterministic parse. Returns None if not confident enough."""
    res = RegexParseResult()

    # Direction
    is_short = bool(_SHORT_WORDS.search(text))
    is_long = bool(_LONG_WORDS.search(text))
    if is_short and not is_long:
        res.side = "sell"
    elif is_long:
        res.side = "buy"
    # else leave default buy but it costs confidence below

    # Pair
    m = _PAIR.search(text)
    if m:
        res.base = m.group("base").upper()
        res.quote = m.group("quote").upper()
    else:
        m2 = _BARE_TICKER.search(text)
        if m2:
            res.base = m2.group("base").upper()

    # Entry (single or zone)
    me = _ENTRY.search(text)
    if me:
        res.entries = [_to_float(g) for g in me.groups() if g]

    # Take profits (possibly several numbers)
    mt = _TP.search(text)
    if mt:
        res.take_profits = [_to_float(g.group()) for g in _NUM_ONLY.finditer(mt.group(1))]

    # Stop loss
    ms = _SL.search(text)
    if ms:
        res.stop_loss = _to_float(ms.group(1))

    # Leverage (informational only for spot)
    ml = _LEV.search(text)
    if ml:
        try:
            res.leverage = _to_float(ml.group(1))
        except ValueError:
            pass

    # Confidence scoring: reward the fields that matter for a safe trade.
    score = 0.0
    if res.base:
        score += 0.4
    if is_long or is_short:
        score += 0.2
    if res.entries:
        score += 0.2
    if res.take_profits:
        score += 0.1
    if res.stop_loss is not None:
        score += 0.1
    res.confidence = round(score, 2)

    # Require at least a symbol plus one price anchor to trust the regex path.
    if res.base and (res.entries or res.take_profits or res.stop_loss is not None):
        return res
    return None
