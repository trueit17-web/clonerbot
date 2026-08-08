"""Thin async CCXT wrapper.

Wraps ccxt.async_support exchanges with just the calls the bot needs: fetch
price, fetch balance, create/cancel spot orders. Keeping this narrow means the
executor doesn't depend on ccxt directly and is trivial to mock in tests/paper.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clonerbot.logging_conf import get_logger

log = get_logger("ccxt")


@dataclass
class ExchangeStatus:
    exchange: str
    reachable: bool          # public API reachable (markets loaded)
    authenticated: bool      # private API works (balance read) → keys valid
    spot: bool               # exchange advertises spot trading
    quote_balance: float     # best total balance in the base quote (e.g. USDT)
    error: str | None = None
    wallets: str = ""        # human summary of non-zero balances across accounts


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

    # Account "wallets" to probe — exchanges like Bybit split funds across
    # Unified / Spot / Funding, and a balance query for the wrong one returns 0.
    _ACCOUNT_TYPES = ("unified", "spot", "funding", "trading", "contract")

    async def check(self, quote: str = "USDT") -> ExchangeStatus:
        """Probe the exchange: public reachability, key validity, spot, balance.

        Balance is read across multiple account types and merged, so funds held
        in a Unified/Funding wallet (common on Bybit) are still found instead of
        wrongly reported as 0.
        """
        ex = self._get()
        try:
            await ex.load_markets()
        except Exception as exc:
            return ExchangeStatus(self.exchange_id, False, False, False, 0.0, str(exc))
        spot = bool((getattr(ex, "has", {}) or {}).get("spot", True))

        best_quote = 0.0
        nonzero: dict[str, float] = {}
        authed = False
        last_err: str | None = None
        # Try the default account, then each named account type.
        for params in [{}] + [{"type": t} for t in self._ACCOUNT_TYPES]:
            try:
                bal = await ex.fetch_balance(params)
            except Exception as exc:
                last_err = str(exc)
                continue
            authed = True
            totals = bal.get("total", {}) or {}
            for asset, amount in totals.items():
                try:
                    amt = float(amount or 0)
                except (TypeError, ValueError):
                    continue
                if amt > 0:
                    # Keep the largest seen per asset (avoids double-counting when
                    # an exchange ignores the account-type param).
                    nonzero[asset] = max(nonzero.get(asset, 0.0), amt)
            best_quote = max(best_quote, float(totals.get(quote, 0.0) or 0.0))

        if not authed:
            return ExchangeStatus(self.exchange_id, True, False, spot, 0.0, last_err)

        top = sorted(nonzero.items(), key=lambda kv: kv[1], reverse=True)[:6]
        wallets = ", ".join(f"{a}: {v:g}" for a, v in top)
        return ExchangeStatus(
            self.exchange_id, True, True, spot, best_quote, None, wallets
        )

    async def close(self) -> None:
        if self._ex is not None:
            await self._ex.close()
