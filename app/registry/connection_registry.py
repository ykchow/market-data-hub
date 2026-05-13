"""Tracks downstream consumers, per-consumer topics, and topic reference counts.

The registry is the control-plane source of truth for *who* wants *which* topics.
Ingestion and the pub/sub broker can use refcounts to decide upstream subscribe
teardown and fan-out attachment; this module only maintains the counts and maps.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _ConsumerState:
    """In-memory record for one downstream connection."""

    topics: set[str] = field(default_factory=set)
    # Monotonic clock anchor for simple "how long connected" metrics.
    connected_at_mono: float = 0.0


class ConnectionRegistry:
    """Authoritative map of consumer subscriptions and per-topic refcounts.

    Each ``(consumer_id, topic)`` pair counts at most once toward the refcount
    for ``topic``. ``subscribe`` / ``unsubscribe`` update that pairing;
    ``unregister_consumer`` removes every topic for that consumer in one step
    (disconnect cleanup).

    All public mutators are ``async`` and run under a single :class:`asyncio.Lock`
    so concurrent WebSocket handlers cannot corrupt refcounts.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._consumers: dict[str, _ConsumerState] = {}
        # topic -> number of distinct consumers currently subscribed
        self._topic_refcounts: dict[str, int] = {}

    async def register_consumer(self, consumer_id: str) -> None:
        """Register a new downstream consumer with an empty subscription set.

        Call this when the transport connection is accepted, before any
        ``subscribe`` calls. Duplicate ids raise :class:`ValueError` so callers
        catch wiring bugs early.

        Args:
            consumer_id: Stable identifier for this connection (e.g. UUID).

        Raises:
            ValueError: If ``consumer_id`` is already registered.
        """
        async with self._lock:
            if consumer_id in self._consumers:
                msg = f"consumer already registered: {consumer_id!r}"
                raise ValueError(msg)
            self._consumers[consumer_id] = _ConsumerState(
                connected_at_mono=time.monotonic(),
            )

    async def unregister_consumer(self, consumer_id: str) -> None:
        """Remove a consumer and all of its topic subscriptions.

        Idempotent: unknown ids are ignored. Refcounts for each topic are
        decremented as if ``unsubscribe`` had been called for every topic,
        which is the required behavior on disconnect.

        Args:
            consumer_id: The consumer to drop.
        """
        async with self._lock:
            state = self._consumers.pop(consumer_id, None)
            if state is None:
                return
            for topic in list(state.topics):
                self._decrement_topic_refcount(topic)
            state.topics.clear()

    async def subscribe(self, consumer_id: str, topic: str) -> None:
        """Add one topic for a consumer and bump the topic refcount if new.

        Idempotent: if the consumer already has this topic, refcount is unchanged.

        Args:
            consumer_id: Must have been registered with ``register_consumer``.
            topic: Topic name (for example ``BTC-USD``).

        Raises:
            ValueError: If the consumer is not registered.
        """
        async with self._lock:
            state = self._consumers.get(consumer_id)
            if state is None:
                msg = f"unknown consumer: {consumer_id!r}"
                raise ValueError(msg)
            if topic in state.topics:
                return
            state.topics.add(topic)
            self._increment_topic_refcount(topic)

    async def unsubscribe(self, consumer_id: str, topic: str) -> None:
        """Remove one topic for a consumer and decrement refcount if it was present.

        Idempotent: missing consumer or topic is a no-op so duplicate control
        messages do not cause errors.

        Args:
            consumer_id: Consumer to adjust.
            topic: Topic to remove for that consumer.
        """
        async with self._lock:
            state = self._consumers.get(consumer_id)
            if state is None:
                return
            if topic not in state.topics:
                return
            state.topics.remove(topic)
            self._decrement_topic_refcount(topic)

    async def get_status(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of registry metrics and membership.

        Useful for REST ``/status`` and operators; values are copies, not live
        references into internal structures.
        """
        async with self._lock:
            now = time.monotonic()
            consumers_payload: list[dict[str, Any]] = []
            for cid, state in sorted(self._consumers.items()):
                consumers_payload.append(
                    {
                        "consumer_id": cid,
                        "topics": sorted(state.topics),
                        "connected_for_seconds": round(
                            max(0.0, now - state.connected_at_mono),
                            3,
                        ),
                    }
                )
            refcounts = dict(sorted(self._topic_refcounts.items()))
            active_topics = [t for t, n in refcounts.items() if n > 0]
            total_pairs = sum(refcounts.values())
            return {
                "active_downstream_consumers": len(self._consumers),
                "topic_reference_counts": refcounts,
                "topics_with_active_demand": active_topics,
                "total_consumer_topic_pairs": total_pairs,
                "consumers": consumers_payload,
            }

    def _increment_topic_refcount(self, topic: str) -> None:
        """Increment refcount for ``topic`` (caller must hold ``_lock``)."""
        self._topic_refcounts[topic] = self._topic_refcounts.get(topic, 0) + 1

    def _decrement_topic_refcount(self, topic: str) -> None:
        """Decrement refcount; drop key when it reaches zero (caller holds ``_lock``)."""
        n = self._topic_refcounts.get(topic, 0) - 1
        if n <= 0:
            self._topic_refcounts.pop(topic, None)
        else:
            self._topic_refcounts[topic] = n
