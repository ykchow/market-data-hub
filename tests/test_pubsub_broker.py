"""PubSubBroker: fan-out, backpressure, and consumer teardown."""

from __future__ import annotations

import asyncio

from app.pubsub.broker import PubSubBroker


def test_remove_consumer_drops_queue_and_topic_links() -> None:
    async def _() -> None:
        broker = PubSubBroker(max_queue_size=10)
        await broker.subscribe_consumer("BTC-USD", "c1")
        await broker.subscribe_consumer("ETH-USD", "c1")
        assert broker.get_consumer_queue("c1") is not None

        await broker.remove_consumer("c1")

        assert broker.get_consumer_queue("c1") is None
        await broker.publish("BTC-USD", {"topic": "BTC-USD"})

    asyncio.run(_())


def test_remove_consumer_idempotent() -> None:
    async def _() -> None:
        broker = PubSubBroker()
        await broker.remove_consumer("ghost")
        await broker.remove_consumer("ghost")

    asyncio.run(_())


def test_remove_consumer_leaves_other_consumers() -> None:
    async def _() -> None:
        broker = PubSubBroker(max_queue_size=10)
        await broker.subscribe_consumer("BTC-USD", "a")
        await broker.subscribe_consumer("BTC-USD", "b")
        await broker.remove_consumer("a")

        assert broker.get_consumer_queue("a") is None
        q_b = broker.get_consumer_queue("b")
        assert q_b is not None
        await broker.publish("BTC-USD", {"x": 1})
        item = await asyncio.wait_for(q_b.get(), timeout=1.0)
        assert item == {"x": 1}

    asyncio.run(_())
