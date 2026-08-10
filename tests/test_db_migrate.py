"""Auto-migration: startup adds columns missing from a pre-existing table."""

from __future__ import annotations

from sqlalchemy import inspect

from clonerbot.db import get_engine, init_db


async def _columns(table: str) -> set[str]:
    engine = get_engine()
    async with engine.begin() as conn:
        return await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns(table)}
        )


async def test_startup_adds_missing_columns():
    engine = get_engine()
    # Simulate an OLD database: drop the current positions table and recreate a
    # legacy one without the futures columns (side/leverage) and without opened_at.
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE positions")
        await conn.exec_driver_sql(
            "CREATE TABLE positions ("
            "id INTEGER PRIMARY KEY, exchange TEXT, symbol TEXT, channel TEXT, "
            "signal_id INTEGER, is_paper BOOLEAN, status TEXT, qty FLOAT, "
            "entry_price FLOAT, stop_loss FLOAT, take_profit FLOAT)"
        )
    before = await _columns("positions")
    assert "side" not in before and "leverage" not in before

    await init_db()  # should ALTER TABLE ADD COLUMN the missing ones

    after = await _columns("positions")
    assert {"side", "leverage", "opened_at", "close_reason"} <= after


async def test_migration_is_idempotent():
    # Running twice must not error (all columns already present the 2nd time).
    await init_db()
    await init_db()
    cols = await _columns("positions")
    assert "side" in cols
