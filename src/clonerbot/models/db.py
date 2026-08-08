"""SQLAlchemy ORM models — the durable audit trail and trading state.

Tables:
  * SignalRecord    — every message seen and what we decided about it (full audit).
  * Position        — open/closed positions with entry, SL/TP and realized PnL.
  * ChannelStats    — per-channel reputation, updated as positions close.
  * EquitySnapshot  — periodic equity marks for drawdown tracking.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SignalRecord(Base):
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("channel", "message_id", name="uq_signal_msg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(128), index=True)
    message_id: Mapped[int] = mapped_column(Integer)
    raw_text: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Parse outcome
    parse_method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    parse_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # Decision outcome: parsed | quarantined | rejected | accepted | executed
    status: Mapped[str] = mapped_column(String(24), index=True, default="parsed")
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    channel: Mapped[str] = mapped_column(String(128), index=True)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    is_paper: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(16), index=True, default="open")  # open | closed

    qty: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)  # tp|sl|manual


class ChannelStats(Base):
    __tablename__ = "channel_stats"

    channel: Mapped[str] = mapped_column(String(128), primary_key=True)
    signals_total: Mapped[int] = mapped_column(Integer, default=0)
    signals_parsed: Mapped[int] = mapped_column(Integer, default=0)
    trades_closed: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    cumulative_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    equity: Mapped[float] = mapped_column(Float)
    realized_pnl_day: Mapped[float] = mapped_column(Float, default=0.0)


class RuntimeSetting(Base):
    """Small key→value store for settings changed at runtime via the bot
    (e.g. the live/paper mode override), so they persist across restarts."""

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ExchangeCredential(Base):
    """Exchange API credentials added at runtime (via the control bot).

    Merged with any exchanges configured in .env at startup. NOTE: secrets are
    stored as-is (like .env). Give keys spot-trade permission only, withdrawals
    disabled, and protect the database file/host accordingly.
    """

    __tablename__ = "exchange_credentials"

    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)  # ccxt id, e.g. "bybit"
    api_key: Mapped[str] = mapped_column(String(256))
    secret: Mapped[str] = mapped_column(String(256))
    password: Mapped[str | None] = mapped_column(String(256), nullable=True)  # passphrase
    enabled: Mapped[bool] = mapped_column(default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ChannelCandidate(Base):
    """A channel found by discovery, tracked through its approval/trust lifecycle.

    status lifecycle:
      discovered → (user /approve) → observing → (auto-promote) → active
                 ↘ (user /reject) → rejected
      active → (auto-demote) → observing
    Only `active` channels trade real money; `observing` ones trade paper-only.
    """

    __tablename__ = "channel_candidates"

    channel: Mapped[str] = mapped_column(String(128), primary_key=True)  # normalized "@name"
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    subscribers: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(64), default="search")  # how it was found
    status: Mapped[str] = mapped_column(String(16), index=True, default="discovered")
    joined: Mapped[bool] = mapped_column(default=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
