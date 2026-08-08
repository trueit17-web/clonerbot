"""Store and parse exchange API credentials added at runtime via the bot.

Credentials live in the DB and are merged with any exchanges from .env at
startup, so keys added through the bot survive restarts.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from clonerbot.db import session_scope
from clonerbot.logging_conf import get_logger
from clonerbot.models.db import ExchangeCredential

log = get_logger("credentials")


@dataclass
class CredView:
    exchange: str
    api_key: str
    secret: str
    password: str | None
    enabled: bool

    def to_ccxt(self) -> dict:
        params = {"apiKey": self.api_key, "secret": self.secret}
        if self.password:
            params["password"] = self.password
        return params


def parse_credentials(text: str) -> tuple[str, str, str | None] | None:
    """Parse 'APIKEY SECRET [PASSPHRASE]' (whitespace/newline separated).

    Returns (api_key, secret, password|None) or None if it doesn't look valid.
    """
    parts = (text or "").split()
    if len(parts) < 2:
        return None
    api_key, secret = parts[0], parts[1]
    password = parts[2] if len(parts) >= 3 else None
    if len(api_key) < 6 or len(secret) < 6:
        return None
    return api_key, secret, password


class CredentialsStore:
    async def upsert(
        self, exchange: str, api_key: str, secret: str, password: str | None = None
    ) -> None:
        exchange = exchange.strip().lower()
        async with session_scope() as s:
            row = await s.get(ExchangeCredential, exchange)
            if row is None:
                row = ExchangeCredential(exchange=exchange, api_key=api_key, secret=secret)
                s.add(row)
            row.api_key = api_key
            row.secret = secret
            row.password = password
            row.enabled = True
        log.info("credentials.saved", exchange=exchange)

    async def delete(self, exchange: str) -> bool:
        async with session_scope() as s:
            row = await s.get(ExchangeCredential, exchange.strip().lower())
            if row is None:
                return False
            await s.delete(row)
            return True

    async def all(self) -> list[CredView]:
        async with session_scope() as s:
            rows = (await s.execute(select(ExchangeCredential))).scalars().all()
            return [
                CredView(r.exchange, r.api_key, r.secret, r.password, r.enabled) for r in rows
            ]
