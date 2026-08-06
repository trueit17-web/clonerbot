"""Signal queue abstraction with dedup.

Two backends behind one interface:
  * InProcessQueue — asyncio.Queue, zero infra, good for a single-process paper run.
  * RedisQueue     — durable list + dedup set, for production / multi-worker.

The ingestor puts RawMessage payloads; the pipeline gets them. Dedup ensures the
same Telegram message is never processed twice (channel:message_id).
"""

from __future__ import annotations

import asyncio
import json
from typing import Protocol

from clonerbot.logging_conf import get_logger
from clonerbot.models.signal import RawMessage

log = get_logger("queue")


class MessageQueue(Protocol):
    async def put(self, msg: RawMessage) -> bool: ...
    async def get(self) -> RawMessage: ...
    async def close(self) -> None: ...


class InProcessQueue:
    def __init__(self, maxsize: int = 1000) -> None:
        self._q: asyncio.Queue[RawMessage] = asyncio.Queue(maxsize=maxsize)
        self._seen: set[str] = set()

    async def put(self, msg: RawMessage) -> bool:
        if msg.dedup_key in self._seen:
            log.debug("dedup.skip", key=msg.dedup_key)
            return False
        self._seen.add(msg.dedup_key)
        await self._q.put(msg)
        return True

    async def get(self) -> RawMessage:
        return await self._q.get()

    async def close(self) -> None:  # nothing to release
        return None


class RedisQueue:
    """Durable queue backed by Redis. Requires the `redis` package and a URL."""

    KEY = "clonerbot:signals"
    SEEN = "clonerbot:seen"

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # local import so redis is optional

        self._redis = redis.from_url(url, decode_responses=True)

    async def put(self, msg: RawMessage) -> bool:
        added = await self._redis.sadd(self.SEEN, msg.dedup_key)
        if not added:
            log.debug("dedup.skip", key=msg.dedup_key)
            return False
        await self._redis.rpush(self.KEY, msg.model_dump_json())
        return True

    async def get(self) -> RawMessage:
        _, payload = await self._redis.blpop(self.KEY)
        return RawMessage(**json.loads(payload))

    async def close(self) -> None:
        await self._redis.aclose()


def build_queue(redis_url: str | None) -> MessageQueue:
    if redis_url:
        log.info("queue.backend", backend="redis")
        return RedisQueue(redis_url)
    log.info("queue.backend", backend="in_process")
    return InProcessQueue()
