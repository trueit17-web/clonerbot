"""Executor — turns approved TradePlans into (paper or live) positions and
manages their lifecycle: open, monitor SL/TP, close, and account bookkeeping.

Design:
  * Open positions are held in memory AND persisted to the `positions` table so
    the bot can reconcile after a restart.
  * A background monitor polls marks for open positions and closes on SL/TP.
  * portfolio_state() builds the snapshot the RiskEngine consumes.
  * Paper vs live differ only at the fill boundary (PaperBroker vs CCXT order).

Equity model:
  paper: broker.cash + Σ(open qty × mark)
  live : free quote balance + Σ(open qty × mark)   (approximation for spot MVP)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from clonerbot.config import Settings
from clonerbot.db import session_scope
from clonerbot.exchange.router import ExchangeRouter
from clonerbot.execution.paper_broker import PaperBroker
from clonerbot.logging_conf import get_logger
from clonerbot.models.db import EquitySnapshot, Position
from clonerbot.risk.risk_engine import PortfolioState, TradePlan
from clonerbot.scoring.channel_scorer import ChannelScorer

log = get_logger("executor")


@dataclass
class OpenPos:
    id: int
    exchange: str
    symbol: str
    channel: str
    qty: float
    entry_price: float
    stop_loss: float
    take_profit: float | None
    cost: float  # cash spent incl. fees (paper) / notional (live)


@dataclass
class Executor:
    settings: Settings
    router: ExchangeRouter
    scorer: ChannelScorer
    paper: PaperBroker = field(init=False)
    open_positions: dict[str, OpenPos] = field(default_factory=dict)  # symbol -> pos
    killed: bool = False
    peak_equity: float = 0.0
    _realized_today: float = 0.0
    _today: str = ""

    def __post_init__(self) -> None:
        self.paper = PaperBroker(self.settings.paper_start_equity)
        self.peak_equity = self.settings.paper_start_equity
        self._today = self._utc_day()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _utc_day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _roll_day(self) -> None:
        today = self._utc_day()
        if today != self._today:
            log.info("executor.day_roll", prev=self._today, new=today, realized=self._realized_today)
            self._today = today
            self._realized_today = 0.0

    @property
    def is_paper(self) -> bool:
        return not self.settings.is_live

    # ------------------------------------------------------------------ equity
    async def _mark(self, symbol: str, fallback: float) -> float:
        price = await self.router.price(symbol)
        return price if price else fallback

    async def equity(self) -> float:
        open_value = 0.0
        for pos in self.open_positions.values():
            open_value += pos.qty * await self._mark(pos.symbol, pos.entry_price)
        if self.is_paper:
            cash = self.paper.cash
        else:
            cash = await self.router.total_quote_equity(self.settings.base_quote)
        return cash + open_value

    async def portfolio_state(self) -> PortfolioState:
        self._roll_day()
        eq = await self.equity()
        self.peak_equity = max(self.peak_equity, eq)
        return PortfolioState(
            equity=eq,
            peak_equity=self.peak_equity,
            realized_pnl_today=self._realized_today,
            open_symbols=set(self.open_positions.keys()),
            open_count=len(self.open_positions),
            killed=self.killed,
        )

    # ------------------------------------------------------------------ open
    async def open_position(self, plan: TradePlan, channel: str, signal_id: int | None) -> OpenPos | None:
        if self.killed:
            log.warning("executor.open_blocked", reason="killed")
            return None
        if plan.symbol in self.open_positions:
            return None

        quote = plan.symbol.split("/")[1]
        exchange_id = "paper"
        fill_price = plan.entry_price

        if self.is_paper:
            fill_price = await self._mark(plan.symbol, plan.entry_price)
            qty = plan.qty
            cost = self.paper.buy(qty, fill_price)
        else:
            client = await self.router.pick(plan.symbol, quote)
            if client is None:
                log.warning("executor.no_exchange", symbol=plan.symbol)
                return None
            qty = await client.amount_to_precision(plan.symbol, plan.qty)
            order = await client.create_market_buy(plan.symbol, qty)
            fill_price = float(order.get("average") or order.get("price") or plan.entry_price)
            qty = float(order.get("filled") or qty)
            cost = qty * fill_price
            exchange_id = client.exchange_id

        async with session_scope() as s:
            row = Position(
                exchange=exchange_id,
                symbol=plan.symbol,
                channel=channel,
                signal_id=signal_id,
                is_paper=self.is_paper,
                status="open",
                qty=qty,
                entry_price=fill_price,
                stop_loss=plan.stop_loss,
                take_profit=plan.take_profit,
            )
            s.add(row)
            await s.flush()
            pos_id = row.id

        pos = OpenPos(
            id=pos_id, exchange=exchange_id, symbol=plan.symbol, channel=channel,
            qty=qty, entry_price=fill_price, stop_loss=plan.stop_loss,
            take_profit=plan.take_profit, cost=cost,
        )
        self.open_positions[plan.symbol] = pos
        log.info(
            "executor.opened", id=pos_id, mode="paper" if self.is_paper else "live",
            symbol=plan.symbol, qty=round(qty, 8), entry=fill_price,
            stop=plan.stop_loss, tp=plan.take_profit,
        )
        return pos

    # ------------------------------------------------------------------ close
    async def close_position(self, symbol: str, reason: str) -> float | None:
        pos = self.open_positions.get(symbol)
        if pos is None:
            return None
        exit_price = await self._mark(symbol, pos.entry_price)

        if self.is_paper:
            proceeds = self.paper.sell(pos.qty, exit_price)
            pnl = proceeds - pos.cost
        else:
            client = self.router.clients.get(pos.exchange)
            if client is not None:
                order = await client.create_market_sell(symbol, pos.qty)
                exit_price = float(order.get("average") or order.get("price") or exit_price)
            pnl = pos.qty * (exit_price - pos.entry_price)

        self._realized_today += pnl

        async with session_scope() as s:
            row = await s.get(Position, pos.id)
            if row is not None:
                row.status = "closed"
                row.closed_at = datetime.now(timezone.utc)
                row.exit_price = exit_price
                row.realized_pnl = pnl
                row.close_reason = reason

        await self.scorer.record_close(pos.channel, pnl)
        del self.open_positions[symbol]
        log.info(
            "executor.closed", id=pos.id, symbol=symbol, reason=reason,
            exit=exit_price, pnl=round(pnl, 2), realized_today=round(self._realized_today, 2),
        )
        return pnl

    async def close_all(self, reason: str = "manual") -> int:
        n = 0
        for symbol in list(self.open_positions.keys()):
            await self.close_position(symbol, reason)
            n += 1
        return n

    # ------------------------------------------------------------------ monitor
    async def monitor_loop(self, stop: asyncio.Event) -> None:
        """Poll open positions and close on SL/TP. Runs until `stop` is set."""
        interval = self.settings.monitor_interval_sec
        while not stop.is_set():
            try:
                await self._check_positions()
                await self._snapshot_equity()
            except Exception as exc:
                log.warning("monitor.error", error=str(exc))
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _check_positions(self) -> None:
        for symbol, pos in list(self.open_positions.items()):
            price = await self.router.price(symbol)
            if price is None:
                continue
            if price <= pos.stop_loss:
                await self.close_position(symbol, "sl")
            elif pos.take_profit is not None and price >= pos.take_profit:
                await self.close_position(symbol, "tp")

    async def _snapshot_equity(self) -> None:
        eq = await self.equity()
        self.peak_equity = max(self.peak_equity, eq)
        async with session_scope() as s:
            s.add(EquitySnapshot(equity=eq, realized_pnl_day=self._realized_today))

    # ------------------------------------------------------------------ restart recovery
    async def recover_open_positions(self) -> None:
        """Reload open positions from the DB after a restart (reconciliation)."""
        from sqlalchemy import select

        async with session_scope() as s:
            rows = (
                await s.execute(
                    select(Position).where(
                        Position.status == "open", Position.is_paper == self.is_paper
                    )
                )
            ).scalars().all()
        for row in rows:
            self.open_positions[row.symbol] = OpenPos(
                id=row.id, exchange=row.exchange, symbol=row.symbol, channel=row.channel,
                qty=row.qty, entry_price=row.entry_price, stop_loss=row.stop_loss or 0.0,
                take_profit=row.take_profit, cost=row.qty * row.entry_price,
            )
        if rows:
            log.info("executor.recovered", count=len(rows))
