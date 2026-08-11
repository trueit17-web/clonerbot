"""Native Bitunix USDT-M futures client.

CCXT does not (yet) support Bitunix, so this implements the subset of the
official Bitunix OpenAPI our executor/router need, matching the CcxtClient
interface (fetch_price, fetch_quote_balance, create_market_buy/sell,
set_leverage, amount_to_precision, check, close).

Reference: https://openapidoc.bitunix.com — base https://fapi.bitunix.com.
Auth (double SHA-256, per the official demo):
    digest = sha256(nonce + timestamp + api_key + query_params + body)
    sign   = sha256(digest + secret_key)
GET query_params = ascii-sorted "".join(k+v); POST body = the exact JSON string
that is sent. Public market endpoints work unsigned.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Any

from clonerbot.exchange.ccxt_client import ExchangeStatus
from clonerbot.logging_conf import get_logger

log = get_logger("bitunix")

BASE_URL = "https://fapi.bitunix.com"


def _nonce() -> str:
    return uuid.uuid4().hex


def _timestamp() -> str:
    return str(int(time.time() * 1000))


def sort_params(params: dict[str, Any]) -> str:
    """Bitunix query digest input: ascii-sorted key+value concatenation."""
    if not params:
        return ""
    return "".join(f"{k}{v}" for k, v in sorted(params.items()))


def sign(api_key: str, secret_key: str, nonce: str, timestamp: str,
         query_params: str = "", body: str = "") -> str:
    digest = hashlib.sha256(
        (nonce + timestamp + api_key + query_params + body).encode()
    ).hexdigest()
    return hashlib.sha256((digest + secret_key).encode()).hexdigest()


class BitunixClient:
    def __init__(self, exchange_id: str, credentials: dict[str, Any],
                 default_type: str = "swap", qty_decimals: int = 3) -> None:
        self.exchange_id = exchange_id  # "bitunix"
        self._key = credentials.get("apiKey") or credentials.get("api_key") or ""
        self._secret = credentials.get("secret") or credentials.get("secret_key") or ""
        self._qty_decimals = qty_decimals
        self._session = None

    # ------------------------------------------------------------------ symbols
    @staticmethod
    def _sym(symbol: str) -> str:
        """'BTC/USDT' → 'BTCUSDT' (Bitunix format)."""
        return symbol.replace("/", "").upper()

    @staticmethod
    def _quote_of(symbol: str) -> str:
        return symbol.split("/")[1] if "/" in symbol else "USDT"

    # ------------------------------------------------------------------ http
    def _get_session(self):
        if self._session is None:
            import aiohttp

            self._session = aiohttp.ClientSession(
                trust_env=True,
                timeout=aiohttp.ClientTimeout(total=20),
                headers={"language": "en-US", "Content-Type": "application/json"},
            )
        return self._session

    def _auth_headers(self, query: str = "", body: str = "") -> dict[str, str]:
        if not self._key:
            return {}
        nonce, ts = _nonce(), _timestamp()
        return {
            "api-key": self._key,
            "sign": sign(self._key, self._secret, nonce, ts, query, body),
            "nonce": nonce,
            "timestamp": ts,
        }

    async def _get(self, path: str, params: dict[str, Any] | None = None,
                   signed: bool = False) -> Any:
        params = params or {}
        headers = self._auth_headers(query=sort_params(params)) if signed else {}
        session = self._get_session()
        async with session.get(BASE_URL + path, params=params, headers=headers) as resp:
            data = await resp.json()
        return self._unwrap(data)

    async def _post(self, path: str, data: dict[str, Any]) -> Any:
        body = json.dumps(data)  # the EXACT string we sign must be the one we send
        headers = self._auth_headers(body=body)
        session = self._get_session()
        async with session.post(BASE_URL + path, data=body, headers=headers) as resp:
            payload = await resp.json()
        return self._unwrap(payload)

    @staticmethod
    def _unwrap(payload: dict) -> Any:
        code = payload.get("code")
        if code not in (0, "0", None):
            raise RuntimeError(f"bitunix error {code}: {payload.get('msg')}")
        return payload.get("data", payload)

    # ------------------------------------------------------------------ market
    async def load_markets(self) -> None:
        # No local market cache needed; a public ticker call verifies reachability.
        await self._get("/api/v1/futures/market/tickers", {"symbols": "BTCUSDT"})

    async def fetch_price(self, symbol: str) -> float:
        data = await self._get("/api/v1/futures/market/tickers", {"symbols": self._sym(symbol)})
        rows = data if isinstance(data, list) else data.get("tickers", data)
        row = rows[0] if isinstance(rows, list) and rows else rows
        for key in ("lastPrice", "last", "markPrice", "close", "indexPrice"):
            v = row.get(key) if isinstance(row, dict) else None
            if v:
                return float(v)
        raise RuntimeError(f"no price for {symbol} on bitunix")

    # ------------------------------------------------------------------ account
    async def _account(self, quote: str = "USDT") -> dict:
        return await self._get("/api/v1/futures/account", {"marginCoin": quote}, signed=True)

    async def fetch_quote_balance(self, quote: str) -> float:
        acc = await self._account(quote)
        return float(acc.get("available", 0) or 0)

    # ------------------------------------------------------------------ orders
    async def set_leverage(self, symbol: str, leverage: float) -> None:
        try:
            await self._post("/api/v1/futures/account/change_leverage", {
                "symbol": self._sym(symbol),
                "marginCoin": self._quote_of(symbol),
                "leverage": int(leverage),
            })
        except Exception as exc:
            log.warning("bitunix.set_leverage_failed", error=str(exc))

    async def _place(self, symbol: str, side: str, qty: float, reduce_only: bool) -> dict:
        data = {
            "symbol": self._sym(symbol),
            "side": side.upper(),                      # BUY / SELL
            "orderType": "MARKET",
            "qty": str(qty),
            "tradeSide": "CLOSE" if reduce_only else "OPEN",
            "effect": "GTC",
            "reduceOnly": reduce_only,
        }
        return await self._post("/api/v1/futures/trade/place_order", data)

    async def create_market_buy(self, symbol: str, qty: float, reduce_only: bool = False) -> dict:
        return await self._place(symbol, "BUY", qty, reduce_only)

    async def create_market_sell(self, symbol: str, qty: float, reduce_only: bool = False) -> dict:
        return await self._place(symbol, "SELL", qty, reduce_only)

    async def fetch_positions(self) -> list[dict]:
        """Open positions on Bitunix, normalized. Empty on error/none."""
        try:
            data = await self._get(
                "/api/v1/futures/position/get_pending_positions", {}, signed=True
            )
        except Exception as exc:
            log.warning("bitunix.fetch_positions_failed", error=str(exc))
            return []
        rows = data if isinstance(data, list) else (
            data.get("positionList") or data.get("list") or data.get("data") or []
        )
        out: list[dict] = []
        for p in rows or []:
            if not isinstance(p, dict):
                continue
            qty = float(p.get("qty") or p.get("positionAmt") or p.get("size") or 0)
            if not qty:
                continue
            side = str(p.get("side") or p.get("positionSide") or "").lower()
            out.append({
                "exchange": "bitunix",
                "symbol": p.get("symbol"),
                "side": "sell" if side in ("sell", "short") else "buy",
                "qty": qty,
                "entry": float(p.get("avgOpenPrice") or p.get("entryPrice")
                               or p.get("avgPrice") or 0),
                "pnl": float(p.get("unrealizedPNL") or p.get("unrealizedPnl") or 0),
                "leverage": float(p.get("leverage") or 0),
            })
        return out

    async def amount_to_precision(self, symbol: str, qty: float) -> float:
        # Floor to the configured decimals (Bitunix rejects over-precise qty).
        factor = 10 ** self._qty_decimals
        return math.floor(qty * factor) / factor

    # ------------------------------------------------------------------ status
    async def check(self, quote: str = "USDT") -> ExchangeStatus:
        try:
            await self.load_markets()
        except Exception as exc:
            return ExchangeStatus("bitunix", False, False, False, 0.0, str(exc))
        try:
            acc = await self._account(quote)
        except Exception as exc:
            return ExchangeStatus("bitunix", True, False, False, 0.0, str(exc))
        available = float(acc.get("available", 0) or 0)
        margin = float(acc.get("margin", 0) or 0)
        total = available + margin
        wallets = f"{quote}: {total:g}" if total else ""
        return ExchangeStatus("bitunix", True, True, False, total, None, wallets, available)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
