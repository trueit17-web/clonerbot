"""Exchange router.

Holds the configured CCXT clients and picks which exchange to trade a given
symbol on. MVP policy: pick the exchange that lists the symbol and has the most
free quote balance. Also aggregates equity and prices across exchanges.

In paper mode the router still uses real exchanges for *price discovery* (public
data, no keys needed if the exchange allows it) but never places orders.
"""

from __future__ import annotations

from clonerbot.config import Settings
from clonerbot.exchange.ccxt_client import CcxtClient, ExchangeStatus
from clonerbot.logging_conf import get_logger

log = get_logger("router")


def build_client(exchange_id: str, creds: dict, default_type: str, settings: Settings):
    """Create the right client for an exchange (native Bitunix, else CCXT)."""
    if exchange_id.strip().lower() == "bitunix":
        from clonerbot.exchange.bitunix import BitunixClient

        return BitunixClient(exchange_id, creds, default_type, settings.bitunix_qty_decimals)
    return CcxtClient(exchange_id, creds, default_type)


class ExchangeRouter:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        # CCXT market type: "swap" (perpetual futures) or "spot".
        self._default_type = "swap" if settings.market.value == "futures" else "spot"
        self._clients: dict = {}
        for ex_id, creds in settings.exchanges.items():
            self._clients[ex_id] = build_client(ex_id, creds, self._default_type, settings)

    @property
    def clients(self) -> dict[str, CcxtClient]:
        return self._clients

    @property
    def has_exchanges(self) -> bool:
        return bool(self._clients)

    def add_client(self, exchange_id: str, creds: dict) -> None:
        """Add or replace a client at runtime (e.g. keys added via the bot)."""
        exchange_id = exchange_id.strip().lower()
        self._clients[exchange_id] = build_client(
            exchange_id, creds, self._default_type, self._s)
        log.info("router.add_client", exchange=exchange_id)

    async def remove_client(self, exchange_id: str) -> bool:
        """Remove a client at runtime (closing its ccxt session)."""
        exchange_id = exchange_id.strip().lower()
        client = self._clients.pop(exchange_id, None)
        if client is None:
            return False
        try:
            await client.close()
        except Exception as exc:
            log.warning("router.close_failed", exchange=exchange_id, error=str(exc))
        log.info("router.remove_client", exchange=exchange_id)
        return True

    async def load_stored(self, store) -> None:
        """Merge DB-stored credentials (added via the bot) into the router."""
        for cred in await store.all():
            if cred.enabled:
                self.add_client(cred.exchange, cred.to_ccxt())

    async def exchange_positions(self) -> list[dict]:
        """Open positions reported by every exchange (the real source of truth)."""
        out: list[dict] = []
        for client in self._clients.values():
            try:
                out += await client.fetch_positions()
            except Exception as exc:
                log.warning("router.positions_failed", exchange=client.exchange_id, error=str(exc))
        return out

    async def status_all(self, quote: str = "USDT") -> list[ExchangeStatus]:
        """Probe every configured exchange for connectivity/auth/balance."""
        out: list[ExchangeStatus] = []
        for client in self._clients.values():
            try:
                out.append(await client.check(quote))
            except Exception as exc:
                out.append(ExchangeStatus(client.exchange_id, False, False, False, 0.0, str(exc)))
        return out

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

    async def max_quote_balance(self, quote: str = "USDT") -> float:
        """Largest free quote balance on any single exchange.

        This is what a new position can actually spend, because an order runs on
        ONE exchange — the one pick() selects (the highest-balance one). Sizing
        off the per-exchange max (not the cross-exchange sum) prevents ordering
        more than the chosen exchange holds."""
        best = 0.0
        for client in self._clients.values():
            try:
                best = max(best, await client.fetch_quote_balance(quote))
            except Exception as exc:
                log.warning("router.balance_failed", exchange=client.exchange_id, error=str(exc))
        return best

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
