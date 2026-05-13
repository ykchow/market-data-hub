"""Tests for ConnectionRegistry: refcounting, subscribe/unsubscribe, disconnect cleanup."""

import asyncio

from app.registry.connection_registry import ConnectionRegistry


def test_registering_consumer() -> None:
    async def _() -> None:
        reg = ConnectionRegistry()
        await reg.register_consumer("c1")
        status = await reg.get_status()
        assert status["active_downstream_consumers"] == 1
        assert len(status["consumers"]) == 1
        assert status["consumers"][0]["consumer_id"] == "c1"
        assert status["consumers"][0]["topics"] == []

    asyncio.run(_())


def test_subscribing_consumer_to_topic() -> None:
    async def _() -> None:
        reg = ConnectionRegistry()
        await reg.register_consumer("c1")
        await reg.subscribe("c1", "BTC-USD")
        status = await reg.get_status()
        assert status["consumers"][0]["topics"] == ["BTC-USD"]
        assert status["topic_reference_counts"] == {"BTC-USD": 1}
        assert status["topics_with_active_demand"] == ["BTC-USD"]
        assert status["total_consumer_topic_pairs"] == 1

    asyncio.run(_())


def test_reference_count_increments_for_distinct_consumers() -> None:
    async def _() -> None:
        reg = ConnectionRegistry()
        await reg.register_consumer("c1")
        await reg.register_consumer("c2")
        await reg.subscribe("c1", "BTC-USD")
        await reg.subscribe("c2", "BTC-USD")
        status = await reg.get_status()
        assert status["topic_reference_counts"]["BTC-USD"] == 2
        assert status["total_consumer_topic_pairs"] == 2

    asyncio.run(_())


def test_multiple_consumers_on_same_topic() -> None:
    async def _() -> None:
        reg = ConnectionRegistry()
        await reg.register_consumer("a")
        await reg.register_consumer("b")
        await reg.subscribe("a", "ETH-USD")
        await reg.subscribe("b", "ETH-USD")
        status = await reg.get_status()
        by_id = {c["consumer_id"]: c["topics"] for c in status["consumers"]}
        assert by_id["a"] == ["ETH-USD"]
        assert by_id["b"] == ["ETH-USD"]
        assert status["topic_reference_counts"]["ETH-USD"] == 2

    asyncio.run(_())


def test_unsubscribe_decrements_refcount() -> None:
    async def _() -> None:
        reg = ConnectionRegistry()
        await reg.register_consumer("c1")
        await reg.register_consumer("c2")
        await reg.subscribe("c1", "BTC-USD")
        await reg.subscribe("c2", "BTC-USD")
        await reg.unsubscribe("c1", "BTC-USD")
        status = await reg.get_status()
        assert status["topic_reference_counts"]["BTC-USD"] == 1
        topics_c1 = next(c["topics"] for c in status["consumers"] if c["consumer_id"] == "c1")
        topics_c2 = next(c["topics"] for c in status["consumers"] if c["consumer_id"] == "c2")
        assert topics_c1 == []
        assert topics_c2 == ["BTC-USD"]

    asyncio.run(_())


def test_disconnect_cleanup_removes_subscriptions() -> None:
    async def _() -> None:
        reg = ConnectionRegistry()
        await reg.register_consumer("c1")
        await reg.subscribe("c1", "BTC-USD")
        await reg.subscribe("c1", "ETH-USD")
        await reg.unregister_consumer("c1")
        status = await reg.get_status()
        assert status["active_downstream_consumers"] == 0
        assert status["consumers"] == []
        assert status["topic_reference_counts"] == {}
        assert status["topics_with_active_demand"] == []
        assert status["total_consumer_topic_pairs"] == 0

    asyncio.run(_())


def test_no_leaked_subscriptions_after_unregister() -> None:
    async def _() -> None:
        reg = ConnectionRegistry()
        await reg.register_consumer("solo")
        await reg.subscribe("solo", "SOL-USD")
        await reg.unregister_consumer("solo")
        status = await reg.get_status()
        assert "SOL-USD" not in status["topic_reference_counts"]
        assert status["total_consumer_topic_pairs"] == 0

    asyncio.run(_())
