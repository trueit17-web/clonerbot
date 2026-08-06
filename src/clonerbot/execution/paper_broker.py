"""Paper broker — simulates fills and tracks virtual quote-currency cash.

Used when CLONERBOT_MODE=paper (the default and recommended first release).
Fills are simulated at the requested price (no slippage model in MVP; a
conservative slippage/fee assumption is a natural next iteration). The broker
holds a single virtual cash balance in the base quote currency; equity is
cash + marked-to-market value of open positions, computed by the executor.
"""

from __future__ import annotations

from clonerbot.logging_conf import get_logger

log = get_logger("paper")

# Simple round-trip fee assumption so paper PnL isn't rosier than reality.
TAKER_FEE = 0.001  # 0.1% per side


class PaperBroker:
    def __init__(self, start_cash: float) -> None:
        self.cash = start_cash
        self.start_cash = start_cash

    def buy(self, qty: float, price: float) -> float:
        """Spend cash to buy qty at price (incl. fee). Returns filled cost."""
        cost = qty * price
        fee = cost * TAKER_FEE
        self.cash -= cost + fee
        log.info("paper.buy", qty=round(qty, 8), price=price, cost=round(cost, 2), cash=round(self.cash, 2))
        return cost + fee

    def sell(self, qty: float, price: float) -> float:
        """Sell qty at price (incl. fee). Returns proceeds credited to cash."""
        proceeds = qty * price
        fee = proceeds * TAKER_FEE
        self.cash += proceeds - fee
        log.info("paper.sell", qty=round(qty, 8), price=price, proceeds=round(proceeds, 2), cash=round(self.cash, 2))
        return proceeds - fee
