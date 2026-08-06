"""Config parsing tests — comma-separated env fields and JSON exchanges.

Regression guard: pydantic-settings JSON-decodes list-typed fields at the env
source, so every CSV field must be NoDecode AND handled by the splitting
validator. A field that is one but not the other raises at startup.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from clonerbot.config import Settings


def _from_env(monkeypatch, **env) -> Settings:
    for k, v in env.items():
        monkeypatch.setenv(f"CLONERBOT_{k}", v)
    return Settings(_env_file=None)


def test_csv_fields_parse_from_env(monkeypatch):
    s = _from_env(
        monkeypatch,
        TG_CHANNELS="@a,@b, @c",
        SYMBOL_WHITELIST="btc,eth",
        CONTROL_ADMIN_IDS="111, 222",
        DISCOVERY_KEYWORDS="crypto signals,futures signals,трейдинг сигналы",
        EXCHANGES='{"bybit":{"apiKey":"x","secret":"y"}}',
    )
    assert s.tg_channels == ["@a", "@b", "@c"]
    assert s.symbol_whitelist == ["BTC", "ETH"]  # uppercased
    assert s.control_admin_ids == [111, 222]
    assert s.discovery_keywords == ["crypto signals", "futures signals", "трейдинг сигналы"]
    assert list(s.exchanges.keys()) == ["bybit"]


def test_empty_exchanges_default(monkeypatch):
    s = _from_env(monkeypatch, EXCHANGES="")
    assert s.exchanges == {}


def test_demote_must_be_below_promote(monkeypatch):
    with pytest.raises(ValidationError):
        _from_env(monkeypatch, DEMOTE_WINRATE="0.6", PROMOTE_MIN_WINRATE="0.5")
