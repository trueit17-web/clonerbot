"""Bitunix native adapter: signing, symbol mapping, order payloads, parsing.

No network — the HTTP helpers (_get/_post) are monkeypatched to capture calls.
"""

from __future__ import annotations

import hashlib

from clonerbot.exchange.bitunix import BitunixClient, sign, sort_params


def _client(**over) -> BitunixClient:
    return BitunixClient("bitunix", {"apiKey": "KEY", "secret": "SECRET"}, **over)


# ------------------------------------------------------------------ signing
def test_sign_is_double_sha256():
    nonce, ts = "abc", "1700000000000"
    query, body = "", '{"a":1}'
    digest = hashlib.sha256((nonce + ts + "KEY" + query + body).encode()).hexdigest()
    expected = hashlib.sha256((digest + "SECRET").encode()).hexdigest()
    assert sign("KEY", "SECRET", nonce, ts, query, body) == expected


def test_sort_params_ascii_sorted_concat():
    assert sort_params({"b": "2", "a": "1"}) == "a1b2"
    assert sort_params({}) == ""


def test_auth_headers_present_with_keys():
    h = _client()._auth_headers(body='{"x":1}')
    assert set(h) == {"api-key", "sign", "nonce", "timestamp"}
    assert h["api-key"] == "KEY" and len(h["sign"]) == 64


def test_no_auth_headers_without_keys():
    c = BitunixClient("bitunix", {})  # no api key → public only
    assert c._auth_headers(body="x") == {}


# --------------------------------------------------------------- symbol map
def test_symbol_conversion():
    assert BitunixClient._sym("BTC/USDT") == "BTCUSDT"
    assert BitunixClient._quote_of("ETH/USDT") == "USDT"


# --------------------------------------------------------------- order payloads
async def test_open_long_payload():
    c = _client()
    captured = {}

    async def fake_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {"orderId": "1"}

    c._post = fake_post
    await c.create_market_buy("BTC/USDT", 0.01, reduce_only=False)
    assert captured["path"].endswith("/trade/place_order")
    d = captured["data"]
    assert d["symbol"] == "BTCUSDT" and d["side"] == "BUY"
    assert d["tradeSide"] == "OPEN" and d["reduceOnly"] is False
    assert d["orderType"] == "MARKET" and d["qty"] == "0.01"


async def test_close_long_is_reduceonly_close():
    c = _client()
    captured = {}

    async def fake_post(path, data):
        captured.update(data)
        return {}

    c._post = fake_post
    await c.create_market_sell("BTC/USDT", 0.01, reduce_only=True)
    assert captured["side"] == "SELL" and captured["tradeSide"] == "CLOSE"
    assert captured["reduceOnly"] is True


async def test_open_short_payload():
    c = _client()
    captured = {}

    async def fake_post(path, data):
        captured.update(data)
        return {}

    c._post = fake_post
    await c.create_market_sell("ETH/USDT", 0.5, reduce_only=False)
    assert captured["side"] == "SELL" and captured["tradeSide"] == "OPEN"


async def test_set_leverage_payload():
    c = _client()
    captured = {}

    async def fake_post(path, data):
        captured["path"] = path
        captured.update(data)
        return {}

    c._post = fake_post
    await c.set_leverage("BTC/USDT", 7)
    assert captured["path"].endswith("/account/change_leverage")
    assert captured["symbol"] == "BTCUSDT" and captured["leverage"] == 7
    assert captured["marginCoin"] == "USDT"


# --------------------------------------------------------------- parsing
async def test_fetch_price_parses_tickers():
    c = _client()

    async def fake_get(path, params=None, signed=False):
        return [{"symbol": "BTCUSDT", "lastPrice": "61234.5", "markPrice": "61230"}]

    c._get = fake_get
    assert await c.fetch_price("BTC/USDT") == 61234.5


async def test_fetch_quote_balance_reads_available():
    c = _client()

    async def fake_get(path, params=None, signed=False):
        return {"available": "1234.56", "margin": "100"}

    c._get = fake_get
    assert await c.fetch_quote_balance("USDT") == 1234.56


async def test_check_reports_tradable_and_total():
    c = _client()

    async def fake_get(path, params=None, signed=False):
        if "account" in path:
            return {"available": "500", "margin": "100"}
        return [{"symbol": "BTCUSDT", "lastPrice": "60000"}]

    c._get = fake_get
    st = await c.check("USDT")
    assert st.reachable and st.authenticated and st.spot is False
    assert st.tradable == 500.0 and st.quote_balance == 600.0


async def test_amount_to_precision_floors():
    c = _client(qty_decimals=3)
    assert await c.amount_to_precision("BTC/USDT", 0.0166666) == 0.016


def test_unwrap_raises_on_error_code():
    import pytest
    with pytest.raises(RuntimeError):
        BitunixClient._unwrap({"code": 10001, "msg": "bad sign"})
    assert BitunixClient._unwrap({"code": 0, "data": {"x": 1}}) == {"x": 1}
