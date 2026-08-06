"""Central configuration, loaded from environment / .env via pydantic-settings.

Every tunable lives here so the rest of the code never reads os.environ directly.
Risk limits are first-class settings — they are the safety envelope that replaces
the human in an autonomous system, so they are validated on startup.
"""

from __future__ import annotations

import json
from enum import Enum
from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- Storage ---
    database_url: str = "sqlite+aiosqlite:///./clonerbot.db"
    redis_url: str = ""

    # --- Telegram ingest (Telethon user session) ---
    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    tg_session: str = "clonerbot_user"
    tg_channels: list[str] = Field(default_factory=list)

    # --- Telegram control bot (aiogram) ---
    control_bot_token: str | None = None
    control_admin_ids: list[int] = Field(default_factory=list)

    # --- Anthropic ---
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"

    # --- Exchanges ---
    exchanges: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # --- Risk envelope ---
    risk_per_trade: float = 0.01
    max_open_positions: int = 5
    max_position_fraction: float = 0.10
    daily_loss_limit: float = 0.05
    max_drawdown: float = 0.20
    symbol_whitelist: list[str] = Field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    default_stop_loss: float = 0.03
    signal_max_age_sec: int = 300

    # ------------------------------------------------------------------
    # Parsers for comma-separated / JSON env values
    # ------------------------------------------------------------------
    @field_validator("tg_channels", "symbol_whitelist", mode="before")
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
        ):
            val = getattr(self, name)
            if not (0 < val <= 1):
                raise ValueError(f"{name} must be a fraction in (0, 1], got {val}")
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
