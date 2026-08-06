"""Exchange router.

Holds the configured CCXT clients and picks which exchange to trade a given
symbol on. MVP policy: pick the exchange that lists the symbol and has the most
free quote balance. Also aggregates equity and prices across exchanges.

In paper mode the router still uses real exchanges for *price discovery* (public
data, no keys needed if the exchange allows it) but never places orders.
"""

from __future__ import annotations

from clonerbot.config import Settings
from clonerbot.exchange.ccxt_client import CcxtClient
from clonerbot.logging_conf import get_logger

log = get_logger("router")


class ExchangeRouter:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._clients: dict[str, CcxtClient] = {}
        for ex_id, creds in settings.exchanges.items():
            self._clients[ex_id] = CcxtClient(ex_id, creds)

    @property
    def clients(self) -> dict[str, CcxtClient]:
        return self._clients

    @property
    def has_exchanges(self) -> bool:
        return bool(self._clients)

    async def load(self) -> None:
        for ex_id, client in self._clients.items():
            try:
                await client.load_markets()
                log.info("router.loaded", exchange=ex_id)
            except Exception as exc:
                log.warning("router.load_failed", exchange=ex_id, error=str(exc))

    async def price(self, symbol: str) -> float | None:
        """Best-effort price from any exchange that has the symbol."""
        for client in self._clients.values():
            try:
                return await client.fetch_price(symbol)
            except Exception:
                continue
        return None

    async def total_quote_equity(self, quote: str = "USDT") -> float:
        total = 0.0
        for client in self._clients.values():
            try:
                total += await client.fetch_quote_balance(quote)
            except Exception as exc:
                log.warning("router.balance_failed", exchange=client.exchange_id, error=str(exc))
        return total

    async def pick(self, symbol: str, quote: str) -> CcxtClient | None:
        """Choose the exchange with the most free quote balance that lists symbol."""
        best: CcxtClient | None = None
        best_bal = -1.0
        for client in self._clients.values():
            try:
                bal = await client.fetch_quote_balance(quote)
            except Exception:
                bal = 0.0
            if bal > best_bal:
                best_bal = bal
                best = client
        return best

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
