"""Tests for the tgstat username extraction (pure parsing, no network)."""

from __future__ import annotations

from clonerbot.discovery.tgstat import TgstatSource


async def test_extracts_usernames(monkeypatch):
    html = """
    <a href="https://t.me/great_signals">Great Signals</a>
    <a href="https://tgstat.ru/en/channel/@whale_alerts/stat">Whales</a>
    <a href="https://t.me/joinchat/AAAA">invite</a>       <!-- reserved, skip -->
    <a href="https://t.me/great_signals">dup</a>           <!-- duplicate, skip -->
    <a href="https://t.me/share/url?u=x">share</a>         <!-- reserved, skip -->
    <a href="https://t.me/cryptoPro99">Crypto Pro</a>
    """
    src = TgstatSource()

    async def fake_fetch(url):
        return html

    monkeypatch.setattr(src, "_fetch", fake_fetch)
    got = await src.search("crypto signals", limit=10)
    assert got == ["@great_signals", "@whale_alerts", "@cryptoPro99"]


async def test_fetch_failure_returns_empty(monkeypatch):
    src = TgstatSource()

    async def fake_fetch(url):
        return None

    monkeypatch.setattr(src, "_fetch", fake_fetch)
    assert await src.search("anything") == []


async def test_respects_limit(monkeypatch):
    html = " ".join(f'<a href="t.me/chan_{i}">x</a>' for i in range(50))
    src = TgstatSource()

    async def fake_fetch(url):
        return html

    monkeypatch.setattr(src, "_fetch", fake_fetch)
    got = await src.search("k", limit=5)
    assert len(got) == 5
