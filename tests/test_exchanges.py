"""Tests for exchange credentials store/parse and runtime router add."""

from __future__ import annotations

from clonerbot.config import Settings
from clonerbot.exchange.credentials import CredentialsStore, parse_credentials
from clonerbot.exchange.router import ExchangeRouter


def _settings(**over) -> Settings:
    base = dict(_env_file=None, exchanges={})
    base.update(over)
    return Settings(**base)


# ------------------------------------------------------------------ parsing
def test_parse_credentials_key_secret():
    assert parse_credentials("APIKEY123 SECRET456") == ("APIKEY123", "SECRET456", None)


def test_parse_credentials_with_passphrase():
    assert parse_credentials("key123456 secret789 passph") == ("key123456", "secret789", "passph")


def test_parse_credentials_rejects_short_or_empty():
    assert parse_credentials("") is None
    assert parse_credentials("only_one_token") is None
    assert parse_credentials("abc 123") is None  # too short


def test_parse_credentials_multiline():
    assert parse_credentials("APIKEY123\nSECRET456\n") == ("APIKEY123", "SECRET456", None)


# -------------------------------------------------------------------- store
async def test_credentials_store_roundtrip():
    store = CredentialsStore()
    await store.upsert("bybit", "keykeykey", "secsecsec", None)
    await store.upsert("okx", "keykeykey2", "secsecsec2", "pass123")
    rows = {c.exchange: c for c in await store.all()}
    assert set(rows) == {"bybit", "okx"}
    assert rows["okx"].password == "pass123"
    assert rows["okx"].to_ccxt() == {
        "apiKey": "keykeykey2", "secret": "secsecsec2", "password": "pass123"
    }
    # upsert overwrites
    await store.upsert("bybit", "newkey1234", "newsecret1", None)
    rows = {c.exchange: c for c in await store.all()}
    assert rows["bybit"].api_key == "newkey1234"
    # delete
    assert await store.delete("bybit") is True
    assert {c.exchange for c in await store.all()} == {"okx"}


async def test_router_load_stored_merges_creds():
    store = CredentialsStore()
    await store.upsert("bybit", "keykeykey", "secsecsec", None)
    router = ExchangeRouter(_settings())
    assert router.has_exchanges is False
    await router.load_stored(store)
    assert "bybit" in router.clients


def test_router_add_client_lowercases():
    router = ExchangeRouter(_settings())
    router.add_client("ByBit", {"apiKey": "k", "secret": "s"})
    assert "bybit" in router.clients
