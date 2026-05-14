"""Topic fan-out to isolated per-consumer asyncio queues.

Each consumer has one bounded ``asyncio.Queue`` shared across all topics they
subscribe to; ``publish`` delivers a copy of the message to every queue for
consumers registered on that topic. Slow readers do not block others because
delivery uses ``put_nowait`` only (see backpressure below).

All ``asyncio.Queue`` access happens on the event loop task (``publish`` is
async) because asyncio queues are not thread-safe.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict


class PubSubBroker:
    """Publishes messages to subscribers by topic.

    Subscribers are identified by ``consumer_id``. Each consumer gets a single
    bounded queue; subscribing to multiple topics multiplexes messages onto that
    queue (callers should embed the topic in the message if consumers need it).
    """

    def __init__(self, max_queue_size: int = 1_000) -> None:
        """Create a broker with the given per-consumer queue capacity.

        ``max_queue_size`` should match application settings (e.g.
        ``Settings.queue_size``) when wired from ``runtime``.
        """
        self._max_queue_size = max_queue_size
        self._lock = asyncio.Lock()
        # topic -> consumers interested in that topic
        self._topic_subscribers: dict[str, set[str]] = defaultdict(set)
        # consumer_id -> one bounded queue for all of that consumer's topics
        self._queues: dict[str, asyncio.Queue[object]] = {}

    async def subscribe_consumer(self, topic: str, consumer_id: str) -> None:
        """Attach ``consumer_id`` to ``topic``; create its queue on first use."""
        async with self._lock:
            if consumer_id not in self._queues:
                self._queues[consumer_id] = asyncio.Queue(maxsize=self._max_queue_size)
            self._topic_subscribers[topic].add(consumer_id)

    async def unsubscribe_consumer(self, topic: str, consumer_id: str) -> None:
        """Remove ``consumer_id`` from ``topic``; drop empty topic buckets."""
        async with self._lock:
            subs = self._topic_subscribers.get(topic)
            if not subs:
                return
            subs.discard(consumer_id)
            if not subs:
                del self._topic_subscribers[topic]

    async def remove_consumer(self, consumer_id: str) -> None:
        """Detach ``consumer_id`` from every topic and delete their delivery queue.

        Used when a downstream connection closes so ``_queues`` does not grow
        without bound across reconnects. Per-topic :meth:`unsubscribe_consumer`
        remains for incremental unsubscribes while the connection stays open.

        Idempotent: missing consumers are ignored.
        """
        async with self._lock:
            for topic, subs in list(self._topic_subscribers.items()):
                if consumer_id not in subs:
                    continue
                subs.discard(consumer_id)
                if not subs:
                    del self._topic_subscribers[topic]
            self._queues.pop(consumer_id, None)

    async def publish(self, topic: str, message: object) -> None:
        """Fan out ``message`` to every queue subscribed to ``topic``.

        Awaits only broker metadata locks, not consumer ``get`` backpressure:
        each delivery is ``put_nowait`` with drop-oldest-on-full (see below).
        """
        async with self._lock:
            consumer_ids = tuple(self._topic_subscribers.get(topic, ()))
            queues = [self._queues[cid] for cid in consumer_ids if cid in self._queues]
        for q in queues:
            self._enqueue_drop_oldest(q, message)

    def get_consumer_queue(self, consumer_id: str) -> asyncio.Queue[object] | None:
        """Return the consumer's queue if it exists (e.g. after at least one subscribe)."""
        return self._queues.get(consumer_id)

    def _enqueue_drop_oldest(self, queue: asyncio.Queue[object], message: object) -> None:
        """Bounded-queue backpressure: prefer freshness over completeness.

        For real-time market data, the latest quote is usually more useful than
        draining a backlog of stale ticks. If the consumer's queue is full,
        we drop the **oldest** item (FIFO head) and enqueue the **latest**
        message. That caps memory, avoids blocking the event loop on ``put``,
        and keeps slow consumers from stalling fan-out to everyone else.

        Trade-off: consumers may lose intermediate updates; they should rely on
        snapshots or message timestamps if they need coherency beyond "most
        recent event stream."
        """
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            # maxsize race: drop again and retry once (e.g. concurrent readers)
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass
