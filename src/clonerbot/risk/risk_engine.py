"""Risk & decision engine.

Given a NormalizedSignal and current account/portfolio state, decide whether to
trade and how large. This is the component that keeps an autonomous bot alive:
it enforces the hard limits that stand in for a human's judgement.

Checks, in order (fail-closed — any failure rejects the trade):
  1. Market/side sanity for spot (we only *buy* to open; sell = skip/close).
  2. Symbol whitelist.
  3. Signal freshness (stale signals are dangerous).
  4. Stop-loss present (or apply the configured default) and sane vs entry.
  5. KILL switch / daily-loss / drawdown halts.
  6. Max concurrent positions and no duplicate open position per symbol.
  7. Position sizing: risk-based, capped by max position fraction, scaled by
     channel reputation.

Sizing model (risk-based):
    risk_amount = equity * risk_per_trade * channel_multiplier
    stop_distance = |entry - stop_loss| / entry           (fractional)
    qty = risk_amount / (entry * stop_distance)
    notional = qty * entry, then capped to equity * max_position_fraction
This means the *loss if the stop is hit* is ~risk_per_trade of equity, which is
the correct way to size — not a flat notional.
"""

from __future__ import annotations

from dataclasses import dataclass

from clonerbot.config import Settings
from clonerbot.logging_conf import get_logger
from clonerbot.models.signal import NormalizedSignal, Side
from clonerbot.scoring.channel_scorer import ChannelScorer

log = get_logger("risk")


@dataclass
class PortfolioState:
    """Snapshot the risk engine reasons about (provided by the executor)."""

    equity: float                 # total account equity in quote currency
    peak_equity: float            # highest equity seen (for drawdown)
    realized_pnl_today: float     # realized PnL since start of UTC day (<=0 is a loss)
    open_symbols: set[str]        # symbols with an open position
    open_count: int               # number of open positions
    killed: bool = False          # manual KILL switch engaged
    # Free base-quote actually available to open a new position now (spot).
    # Defaults to unlimited so callers that don't set it are unconstrained.
    tradable: float = float("inf")


@dataclass
class TradePlan:
    approved: bool
    reason: str
    symbol: str = ""
    side: Side = Side.buy
    qty: float = 0.0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float | None = None
    channel_multiplier: float = 1.0


class RiskEngine:
    def __init__(self, settings: Settings, scorer: ChannelScorer, locks=None) -> None:
        self._s = settings
        self._scorer = scorer
        self._locks = locks  # LockStore | None — protections; None disables checks

    async def evaluate(
        self,
        signal: NormalizedSignal,
        state: PortfolioState,
        market_price: float,
    ) -> TradePlan:
        s = self._s
        reject = lambda why: TradePlan(False, why)  # noqa: E731

        # 0) Global halts (fail-closed)
        if state.killed:
            return reject("KILL switch engaged")
        if state.equity <= 0:
            return reject("no equity")

        # 0b) Protections: refuse while a lock (global / channel / symbol) is active.
        if self._locks is not None:
            from clonerbot.risk.protections import GLOBAL_KEY, channel_key, symbol_key

            locked = await self._locks.active_reason(
                [GLOBAL_KEY, channel_key(signal.channel), symbol_key(signal.symbol)]
            )
            if locked:
                return reject(f"locked: {locked}")

        # 1) Spot semantics: we only open with buys. Sells are handled as
        #    close-signals elsewhere, not as new positions here.
        if signal.side is Side.sell:
            return reject("spot MVP: sell signal is not a new position")

        # 2) Whitelist
        if s.symbol_whitelist and signal.base not in s.symbol_whitelist:
            return reject(f"{signal.base} not in whitelist")

        # 3) Freshness
        if signal.age_seconds > s.signal_max_age_sec:
            return reject(f"stale signal ({signal.age_seconds:.0f}s old)")

        # 4) Drawdown & daily loss halts
        if state.peak_equity > 0:
            drawdown = (state.peak_equity - state.equity) / state.peak_equity
            if drawdown >= s.max_drawdown:
                return reject(f"max drawdown reached ({drawdown:.1%})")
        daily_loss_frac = -state.realized_pnl_today / state.equity
        if daily_loss_frac >= s.daily_loss_limit:
            return reject(f"daily loss limit reached ({daily_loss_frac:.1%})")

        # 5) Concurrency & duplicates
        if signal.symbol in state.open_symbols:
            return reject("position already open for symbol")
        if state.open_count >= s.max_open_positions:
            return reject(f"max open positions ({s.max_open_positions})")

        # 6) Entry & stop resolution
        entry = signal.reference_entry() or market_price
        if entry <= 0:
            return reject("no usable entry price")
        stop = signal.stop_loss
        if stop is None:
            stop = entry * (1 - s.default_stop_loss)  # apply configured default
        if stop <= 0 or stop >= entry:
            return reject("invalid stop-loss (must be > 0 and below entry for spot buy)")

        stop_distance = (entry - stop) / entry
        if stop_distance < 1e-4:
            return reject("stop too tight")

        # 7) Sizing — multiplier is learned from the channel's track record
        #    (Edge-style expectancy). A non-positive multiplier means the channel
        #    has proven unprofitable → skip it.
        mult = await self._scorer.multiplier(signal.channel)
        if mult <= 0:
            return reject("channel expectancy non-positive")
        risk_amount = state.equity * s.risk_per_trade * mult
        qty = risk_amount / (entry * stop_distance)

        # The position may never cost more than what is actually free to trade
        # right now (spot base-quote), nor more than the per-position fraction.
        if state.tradable <= 0:
            return reject("no funds available to trade")
        notional = qty * entry
        cap = min(state.equity * s.max_position_fraction, state.tradable)
        if notional > cap:
            qty = cap / entry
            notional = cap
        if qty <= 0 or notional <= 0:
            return reject("computed qty <= 0")

        tp = signal.take_profits[0] if signal.take_profits else None

        plan = TradePlan(
            approved=True,
            reason="approved",
            symbol=signal.symbol,
            side=Side.buy,
            qty=qty,
            entry_price=entry,
            stop_loss=stop,
            take_profit=tp,
            channel_multiplier=mult,
        )
        log.info(
            "risk.approved",
            symbol=plan.symbol,
            qty=round(qty, 8),
            notional=round(notional, 2),
            entry=entry,
            stop=round(stop, 8),
            tp=tp,
            channel_mult=mult,
        )
        return plan
