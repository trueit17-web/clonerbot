"""Central configuration, loaded from environment / .env via pydantic-settings.

Every tunable lives here so the rest of the code never reads os.environ directly.
Risk limits are first-class settings — they are the safety envelope that replaces
the human in an autonomous system, so they are validated on startup.
"""

from __future__ import annotations

import json
from enum import Enum
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Mode(str, Enum):
    paper = "paper"
    live = "live"


class Market(str, Enum):
    spot = "spot"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLONERBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Mode ---
    mode: Mode = Mode.paper
    market: Market = Market.spot
    # Virtual starting equity (quote currency) for paper mode.
    paper_start_equity: float = 10_000.0
    # How often (seconds) the executor polls open positions for SL/TP.
    monitor_interval_sec: int = 15
    # Quote currency the risk engine sizes and tracks equity in.
    base_quote: str = "USDT"

    # --- Storage ---
    database_url: str = "sqlite+aiosqlite:///./clonerbot.db"
    redis_url: str = ""

    # --- Telegram ingest (Telethon user session) ---
    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    tg_session: str = "clonerbot_user"
    # NoDecode: keep pydantic-settings from JSON-parsing the env value so our
    # comma-separated string reaches the validator below (e.g. "@a,@b").
    tg_channels: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Telegram control bot (aiogram) ---
    control_bot_token: str | None = None
    control_admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    # Push a message to admins on each trade open/close and channel promotion.
    notify_trades: bool = True

    # --- Anthropic ---
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"

    # --- Exchanges ---
    # NoDecode + custom validator so an empty env value means {} (not a JSON error).
    exchanges: Annotated[dict[str, dict[str, Any]], NoDecode] = Field(default_factory=dict)

    # --- Risk envelope ---
    risk_per_trade: float = 0.01
    max_open_positions: int = 5
    max_position_fraction: float = 0.10
    daily_loss_limit: float = 0.05
    max_drawdown: float = 0.20
    symbol_whitelist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTC", "ETH", "SOL"]
    )
    default_stop_loss: float = 0.03
    signal_max_age_sec: int = 300
    # Trailing stop: as price rises, the stop ratchets up to price*(1-this).
    # 0 disables trailing (fixed stop only).
    trailing_stop_pct: float = 0.0

    # --- Paper simulation realism ---
    # Assumed adverse slippage per fill (fraction): buys fill higher, sells lower.
    paper_slippage: float = 0.0005

    # --- Channel discovery (auto-find signal channels) ---
    # Master switch; OFF by default so existing deployments are unaffected.
    discovery_enabled: bool = False
    # Keywords to search public Telegram channels for (comma-separated).
    discovery_keywords: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["crypto signals", "futures signals", "trading signals"]
    )
    # How often to scan for new candidates.
    discovery_interval_sec: int = 3600
    # Also scrape tgstat.ru catalog/search as a discovery source (best-effort,
    # may be rate-limited/blocked; candidates still go through observe→promote).
    discovery_use_tgstat: bool = False
    # Ignore channels smaller than this (weak signal-to-noise, likely junk).
    discovery_min_subscribers: int = 1000
    # Cap candidates surfaced per scan.
    discovery_max_candidates_per_scan: int = 20
    # Minimum gap between joins — protects the user account from Telegram bans.
    join_cooldown_sec: int = 1800
    # Promotion gate: a discovered channel trades REAL money only after this many
    # closed paper trades with at least this win rate (and positive cumulative PnL).
    promote_min_trades: int = 10
    promote_min_winrate: float = 0.55
    # Auto-demote back to observe if win rate falls below this after enough trades.
    demote_winrate: float = 0.40

    # ------------------------------------------------------------------
    # Parsers for comma-separated / JSON env values
    # ------------------------------------------------------------------
    @field_validator("tg_channels", "symbol_whitelist", "discovery_keywords", mode="before")
    @classmethod
    def _split_csv(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("control_admin_ids", mode="before")
    @classmethod
    def _split_int_csv(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("exchanges", mode="before")
    @classmethod
    def _parse_exchanges(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return {}
            return json.loads(v)
        return v

    @field_validator("symbol_whitelist", mode="after")
    @classmethod
    def _upper_whitelist(cls, v: list[str]) -> list[str]:
        return [s.upper() for s in v]

    # ------------------------------------------------------------------
    # Cross-field validation of the risk envelope
    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def _validate_risk(self) -> Settings:
        for name in (
            "risk_per_trade",
            "max_position_fraction",
            "daily_loss_limit",
            "max_drawdown",
            "default_stop_loss",
            "promote_min_winrate",
            "demote_winrate",
        ):
            val = getattr(self, name)
            if not (0 < val <= 1):
                raise ValueError(f"{name} must be a fraction in (0, 1], got {val}")
        if self.demote_winrate >= self.promote_min_winrate:
            raise ValueError("demote_winrate must be below promote_min_winrate")
        # These may be 0 (feature disabled), so they allow [0, 1).
        for name in ("trailing_stop_pct", "paper_slippage"):
            val = getattr(self, name)
            if not (0 <= val < 1):
                raise ValueError(f"{name} must be a fraction in [0, 1), got {val}")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be >= 1")
        if self.risk_per_trade > self.max_position_fraction:
            raise ValueError("risk_per_trade cannot exceed max_position_fraction")
        return self

    @property
    def is_live(self) -> bool:
        return self.mode is Mode.live


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so config is parsed once per process."""
    return Settings()
