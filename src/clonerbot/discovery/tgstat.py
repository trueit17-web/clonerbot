"""Best-effort tgstat.ru discovery source.

tgstat.ru is a public Telegram-channel catalog. It has no free structured API,
so this scrapes its search pages and extracts channel usernames from the HTML
(t.me/<name> and tgstat.ru/channel/@<name> links). It is intentionally
defensive: any network/parse failure returns an empty list rather than raising,
because discovery is a nice-to-have and must never take the bot down.

Notes:
  * Scraping may be rate-limited or blocked, and can break if tgstat changes its
    markup — treat results as *candidates only*. Trust is still earned later via
    paper trading + PromotionService.
  * Subscriber counts aren't reliably parseable here, so candidates come through
    with subscribers=0 and skip the size floor.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from clonerbot.logging_conf import get_logger

log = get_logger("tgstat")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Usernames from t.me/<name> or tgstat.ru/channel/@<name> (also /en/channel/...).
_USERNAME_RE = re.compile(
    r"(?:t\.me/|tgstat\.ru/(?:[a-z]{2}/)?channel/@)([A-Za-z][A-Za-z0-9_]{3,31})"
)
# Telegram reserved / non-channel paths to ignore.
_RESERVED = {
    "joinchat", "share", "iv", "addstickers", "proxy", "socks", "s", "c",
    "tgstat", "telegram", "durov",
}


class TgstatSource:
    def __init__(self, base: str = "https://tgstat.ru") -> None:
        self._base = base

    async def search(self, keyword: str, limit: int = 20) -> list[str]:
        """Return up to `limit` candidate channel usernames (as '@name')."""
        url = f"{self._base}/search?q={quote(keyword)}"
        html = await self._fetch(url)
        if not html:
            return []
        found: list[str] = []
        seen: set[str] = set()
        for m in _USERNAME_RE.finditer(html):
            name = m.group(1)
            low = name.lower()
            if low in _RESERVED or low in seen:
                continue
            seen.add(low)
            found.append(f"@{name}")
            if len(found) >= limit:
                break
        log.info("tgstat.search", keyword=keyword, found=len(found))
        return found

    async def _fetch(self, url: str) -> str | None:
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=20)
            # trust_env=True so the configured HTTPS proxy is honored.
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=True, headers={"User-Agent": _UA}
            ) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        log.warning("tgstat.http", status=resp.status, url=url)
                        return None
                    return await resp.text()
        except Exception as exc:
            log.warning("tgstat.fetch_failed", error=str(exc))
            return None
