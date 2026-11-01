"""In-process pub/sub for trajectory live SSE fan-out."""

from __future__ import annotations

import asyncio
from typing import Any


class TrajectoryLiveBus:
    def __init__(self, *, subscriber_queue_size: int = 256) -> None:
        self._subscriber_queue_size = subscriber_queue_size
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._kinds: dict[asyncio.Queue[dict[str, Any]], frozenset[str]] = {}
        self._overflowed: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(
        self, thread_id: str, *, kinds: list[str] | None = None
    ) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        self._subscribers.setdefault(thread_id, set()).add(queue)
        if kinds is not None:
            self._kinds[queue] = frozenset(kinds)
        return queue

    def unsubscribe(self, thread_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._kinds.pop(queue, None)
        self._overflowed.discard(queue)
        queues = self._subscribers.get(thread_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            del self._subscribers[thread_id]

    def publish(self, thread_id: str, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(thread_id, ())):
            kinds = self._kinds.get(queue)
            if kinds is not None and message.get("kind") not in kinds:
                continue
            if queue.full():
                if kinds is not None:
                    self._overflowed.add(queue)
                queue.get_nowait()
            queue.put_nowait(message)

    def overflowed(self, queue: asyncio.Queue[dict[str, Any]]) -> bool:
        """A filtered consumer must reconnect from its last delivered cursor."""
        return queue in self._overflowed
