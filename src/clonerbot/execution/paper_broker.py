"""Paper broker — simulates fills and tracks virtual quote-currency cash.

Used when CLONERBOT_MODE=paper (the default and recommended first release).
Fills model two real costs so paper PnL isn't rosier than live:
  * taker fee per side
  * adverse slippage — a buy fills slightly ABOVE the mark, a sell slightly
    BELOW it (you never get filled at a price that favors you).

buy()/sell() return the effective fill price (after slippage) so the executor
records the true entry/exit, and the cash delta (after fee). Equity is
cash + marked-to-market open positions, computed by the executor.
"""

from __future__ import annotations

from clonerbot.logging_conf import get_logger

log = get_logger("paper")

# Round-trip taker fee assumption.
TAKER_FEE = 0.001  # 0.1% per side


class PaperBroker:
    def __init__(self, start_cash: float, slippage: float = 0.0) -> None:
        self.cash = start_cash
        self.start_cash = start_cash
        self.slippage = slippage

    def buy(self, qty: float, mark: float) -> tuple[float, float]:
        """Buy qty around `mark`. Returns (fill_price, cost_incl_fee)."""
        fill = mark * (1 + self.slippage)  # adverse: pay a bit more
        cost = qty * fill
        fee = cost * TAKER_FEE
        total = cost + fee
        self.cash -= total
        log.info(
            "paper.buy", qty=round(qty, 8), mark=mark, fill=round(fill, 8),
            cost=round(total, 2), cash=round(self.cash, 2),
        )
        return fill, total

    def sell(self, qty: float, mark: float) -> tuple[float, float]:
        """Sell qty around `mark`. Returns (fill_price, proceeds_incl_fee)."""
        fill = mark * (1 - self.slippage)  # adverse: receive a bit less
        proceeds = qty * fill
        fee = proceeds * TAKER_FEE
        net = proceeds - fee
        self.cash += net
        log.info(
            "paper.sell", qty=round(qty, 8), mark=mark, fill=round(fill, 8),
            proceeds=round(net, 2), cash=round(self.cash, 2),
        )
        return fill, net

    # ------------------------------------------------------------------ futures
    def futures_open(self, side: str, qty: float, mark: float,
                     leverage: float) -> tuple[float, float, float]:
        """Open a leveraged position. Returns (fill_price, margin, open_fee).

        Adverse slippage: a long fills a bit higher, a short a bit lower.
        Only the margin (notional/leverage) plus fee leaves cash."""
        fill = mark * (1 + self.slippage) if side == "buy" else mark * (1 - self.slippage)
        notional = qty * fill
        margin = notional / leverage
        fee = notional * TAKER_FEE
        self.cash -= margin + fee
        log.info("paper.fut_open", side=side, qty=round(qty, 8), fill=round(fill, 8),
                 margin=round(margin, 2), cash=round(self.cash, 2))
        return fill, margin, fee

    def futures_close(self, side: str, qty: float, entry: float, mark: float,
                      margin: float, open_fee: float) -> tuple[float, float]:
        """Close a leveraged position. Returns (exit_fill, realized_pnl).

        realized_pnl is net of both fees; cash gets the margin back plus PnL."""
        # Exit is the opposite action: long sells (lower), short buys (higher).
        fill = mark * (1 - self.slippage) if side == "buy" else mark * (1 + self.slippage)
        gross = qty * (fill - entry) if side == "buy" else qty * (entry - fill)
        close_fee = qty * fill * TAKER_FEE
        self.cash += margin + gross - close_fee
        realized = gross - close_fee - open_fee
        log.info("paper.fut_close", side=side, qty=round(qty, 8), fill=round(fill, 8),
                 pnl=round(realized, 2), cash=round(self.cash, 2))
        return fill, realized
