"""CCXT-backed historical price source for backtests (public OHLCV, no keys)."""

from __future__ import annotations

from clonerbot.backtest.engine import Candle
from clonerbot.logging_conf import get_logger

log = get_logger("history")


class CcxtHistory:
    def __init__(self, exchange_id: str = "binance") -> None:
        self.exchange_id = exchange_id
        self._ex = None

    def _get(self):
        if self._ex is None:
            import ccxt.async_support as ccxt

            if not hasattr(ccxt, self.exchange_id):
                raise ValueError(f"Unknown exchange '{self.exchange_id}'")
            self._ex = getattr(ccxt, self.exchange_id)({"enableRateLimit": True})
        return self._ex

    async def ohlcv(self, symbol: str, since_ms: int, timeframe: str, limit: int) -> list[Candle]:
        ex = self._get()
        return await ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit)

    async def close(self) -> None:
        if self._ex is not None:
            await self._ex.close()
