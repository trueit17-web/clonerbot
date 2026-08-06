"""Thin async CCXT wrapper.

Wraps ccxt.async_support exchanges with just the calls the bot needs: fetch
price, fetch balance, create/cancel spot orders. Keeping this narrow means the
executor doesn't depend on ccxt directly and is trivial to mock in tests/paper.
"""

from __future__ import annotations

from typing import Any

from clonerbot.logging_conf import get_logger

log = get_logger("ccxt")


class CcxtClient:
    def __init__(self, exchange_id: str, credentials: dict[str, Any]) -> None:
        self.exchange_id = exchange_id
        self._creds = credentials
        self._ex = None

    def _get(self):
        if self._ex is None:
            import ccxt.async_support as ccxt

            if not hasattr(ccxt, self.exchange_id):
                raise ValueError(f"Unknown exchange '{self.exchange_id}'")
            klass = getattr(ccxt, self.exchange_id)
            params = dict(self._creds)
            params.setdefault("enableRateLimit", True)
            params.setdefault("options", {})
            params["options"].setdefault("defaultType", "spot")
            self._ex = klass(params)
        return self._ex

    async def load_markets(self) -> None:
        await self._get().load_markets()

    async def fetch_price(self, symbol: str) -> float:
        ticker = await self._get().fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close") or ticker.get("bid")
        if not price:
            raise RuntimeError(f"no price for {symbol} on {self.exchange_id}")
        return float(price)

    async def fetch_quote_balance(self, quote: str) -> float:
        bal = await self._get().fetch_balance()
        free = bal.get("free", {}) or {}
        return float(free.get(quote, 0.0) or 0.0)

    async def create_market_buy(self, symbol: str, qty: float) -> dict:
        return await self._get().create_order(symbol, "market", "buy", qty)

    async def create_market_sell(self, symbol: str, qty: float) -> dict:
        return await self._get().create_order(symbol, "market", "sell", qty)

    async def amount_to_precision(self, symbol: str, qty: float) -> float:
        try:
            return float(self._get().amount_to_precision(symbol, qty))
        except Exception:
            return qty

    async def close(self) -> None:
        if self._ex is not None:
            await self._ex.close()
