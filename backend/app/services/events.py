"""Thread-safe, in-process event bus for live UI updates.

The loop engine, execution service, and reconciler all run inside worker
threads, while the WebSocket endpoint serves browsers on the asyncio event
loop. Events are therefore published from any thread and fanned out to
subscriber queues through ``loop.call_soon_threadsafe``.

Publishing is fire-and-forget by contract: a dead loop, a full queue, or a
missing attachment must never break a trading operation. Events are a
convenience channel for the UI; the audit ledger remains the system of record.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from typing import Any

_logger = logging.getLogger(__name__)

_QUEUE_LIMIT = 256


class EventBus:
    """Fan-out hub connecting sync producers to async WebSocket consumers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the asyncio loop that WebSocket senders run on (lifespan startup)."""
        with self._lock:
            self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a bounded per-client queue; one client may never starve the rest."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Safe from any thread. Never raises; drops events no consumer can take."""
        event = {"type": event_type, "ts": datetime.now(UTC).isoformat(), "payload": payload or {}}
        with self._lock:
            loop, subscribers = self._loop, list(self._subscribers)
        for queue in subscribers:
            self._dispatch(loop, queue, event)

    def _dispatch(self, loop: asyncio.AbstractEventLoop | None, queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._offer, queue, event)
        except RuntimeError:
            # Loop stopped between check and schedule: the consumer is gone.
            pass

    @staticmethod
    def _offer(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        """Drop-oldest offer: live readings matter more than stale history."""
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


event_bus = EventBus()


def publish_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    """Fire-and-forget publish that trading code may call without try/except."""
    try:
        event_bus.publish(event_type, payload)
    except Exception:
        _logger.debug("Dropped %s event: bus failure", event_type, exc_info=True)
