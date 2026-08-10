"""The processing pipeline: raw message → parse → audit → risk → execute.

This is the autonomous decision loop. Every message and the decision made about
it is written to the `signals` table, giving a complete, queryable audit trail —
essential for trusting (and debugging) a system that trades without a human.
"""

from __future__ import annotations

from clonerbot.config import Settings
from clonerbot.db import session_scope
from clonerbot.execution.executor import Executor
from clonerbot.logging_conf import get_logger
from clonerbot.models.db import SignalRecord
from clonerbot.models.signal import RawMessage
from clonerbot.parser.signal_parser import ParseOutcome, SignalParser
from clonerbot.risk.risk_engine import RiskEngine
from clonerbot.scoring.channel_scorer import ChannelScorer

log = get_logger("pipeline")


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        parser: SignalParser,
        risk: RiskEngine,
        executor: Executor,
        scorer: ChannelScorer,
        gate=None,
        shadow_executor: Executor | None = None,
    ) -> None:
        self._s = settings
        self._parser = parser
        self._risk = risk
        self._executor = executor          # ACTIVE channels (real in live mode)
        self._scorer = scorer
        self._gate = gate                  # None → single-executor legacy behavior
        self._shadow = shadow_executor      # OBSERVING channels (always paper)

    async def _route(self, channel: str) -> Executor:
        """Pick the executor: ACTIVE → main, OBSERVING → shadow (paper-only)."""
        if self._gate is None or self._shadow is None:
            return self._executor
        return self._executor if await self._gate.trades_real(channel) else self._shadow

    async def handle(self, msg: RawMessage) -> None:
        outcome = await self._parser.parse(msg)
        await self._scorer.record_signal(msg.channel, parsed=outcome.signal is not None)

        if outcome.signal is None:
            await self._audit(msg, outcome, status="quarantined")
            log.info("pipeline.quarantined", key=msg.dedup_key, reason=outcome.reason)
            return

        signal = outcome.signal
        signal_id = await self._audit(msg, outcome, status="parsed")

        # Route to the real (ACTIVE) or shadow (OBSERVING, paper) executor.
        executor = await self._route(signal.channel)
        shadow = executor is self._shadow

        # Current market price for sizing (fallback to signal entry).
        market_price = await executor.router.price(signal.symbol)
        if market_price is None:
            market_price = signal.reference_entry() or 0.0

        state = await executor.portfolio_state()
        plan = await self._risk.evaluate(signal, state, market_price)

        if not plan.approved:
            await self._update_status(signal_id, "rejected", plan.reason)
            log.info("pipeline.rejected", key=msg.dedup_key, reason=plan.reason)
            return

        pos = await executor.open_position(plan, signal.channel, signal_id)
        if pos is None:
            await self._update_status(signal_id, "rejected", "executor declined")
            return
        status = "shadow" if shadow else "executed"
        await self._update_status(signal_id, status, f"pos#{pos.id}")
        log.info("pipeline.executed", key=msg.dedup_key, position_id=pos.id, shadow=shadow)

    # ------------------------------------------------------------------ audit
    async def _audit(self, msg: RawMessage, outcome: ParseOutcome, status: str) -> int:
        import json

        sig = outcome.signal
        async with session_scope() as s:
            row = SignalRecord(
                channel=msg.channel,
                message_id=msg.message_id,
                raw_text=msg.text,
                posted_at=msg.posted_at,
                parse_method=sig.parse_method.value if sig else None,
                parse_confidence=sig.parse_confidence if sig else None,
                symbol=sig.symbol if sig else None,
                side=sig.side.value if sig else None,
                entries=json.dumps(sig.entries) if sig else None,
                take_profits=json.dumps(sig.take_profits) if sig else None,
                stop_loss=sig.stop_loss if sig else None,
                status=status,
                decision_reason=outcome.reason,
            )
            s.add(row)
            await s.flush()
            return row.id

    async def _update_status(self, signal_id: int, status: str, reason: str) -> None:
        async with session_scope() as s:
            row = await s.get(SignalRecord, signal_id)
            if row is not None:
                row.status = status
                row.decision_reason = reason
