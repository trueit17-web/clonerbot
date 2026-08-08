"""Runtime settings persisted in the DB (key/value), applied on startup.

Currently used for the live/paper mode override that the control bot can toggle
without editing .env. Kept tiny and dependency-free on purpose.
"""

from __future__ import annotations

from clonerbot.db import session_scope
from clonerbot.models.db import RuntimeSetting

MODE_KEY = "mode"


async def get_flag(key: str) -> str | None:
    async with session_scope() as s:
        row = await s.get(RuntimeSetting, key)
        return row.value if row else None


async def set_flag(key: str, value: str) -> None:
    async with session_scope() as s:
        row = await s.get(RuntimeSetting, key)
        if row is None:
            s.add(RuntimeSetting(key=key, value=value))
        else:
            row.value = value
