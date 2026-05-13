"""Tests for SnapshotStore: apply events, reads, staleness, unknown topics."""

from datetime import datetime, timedelta, timezone

import pytest

from app.cache.snapshot_store import SnapshotStore
from app.models.market_data import MarketEvent, MarketEventType


def test_snapshot_created_from_market_event() -> None:
    store = SnapshotStore(stale_threshold_seconds=3600.0)
    ts = datetime.now(timezone.utc)
    event = MarketEvent(
        topic="BTC-USD",
        event_type=MarketEventType.TRADE,
        price=100_000.0,
        best_bid=99_999.0,
        best_ask=100_001.0,
        received_at=ts,
    )

    store.apply_event(event)
    snap = store.get_snapshot("BTC-USD")

    assert snap is not None
    assert snap.topic == "BTC-USD"
    assert snap.last_trade_price == 100_000.0
    assert snap.best_bid == 99_999.0
    assert snap.best_ask == 100_001.0
    assert snap.mid_price == pytest.approx(100_000.0)
    assert snap.updated_at == ts
    assert snap.stale is False


def test_get_snapshot_returns_latest_state() -> None:
    store = SnapshotStore(stale_threshold_seconds=3600.0)
    base = datetime.now(timezone.utc)

    store.apply_event(
        MarketEvent(
            topic="ETH-USD",
            event_type=MarketEventType.TICKER,
            price=2_000.0,
            best_bid=1_999.0,
            best_ask=2_001.0,
            received_at=base,
        )
    )
    later = base + timedelta(seconds=1)
    store.apply_event(
        MarketEvent(
            topic="ETH-USD",
            event_type=MarketEventType.TRADE,
            price=2_050.0,
            best_bid=2_049.0,
            best_ask=2_051.0,
            received_at=later,
        )
    )

    snap = store.get_snapshot("ETH-USD")
    assert snap is not None
    assert snap.last_trade_price == 2_050.0
    assert snap.best_bid == 2_049.0
    assert snap.best_ask == 2_051.0
    assert snap.updated_at == later


def test_list_snapshots_returns_all_snapshots_sorted_by_topic() -> None:
    store = SnapshotStore(stale_threshold_seconds=3600.0)
    t0 = datetime.now(timezone.utc)

    store.apply_event(
        MarketEvent(
            topic="SOL-USD",
            event_type=MarketEventType.TICKER,
            price=150.0,
            received_at=t0,
        )
    )
    store.apply_event(
        MarketEvent(
            topic="BTC-USD",
            event_type=MarketEventType.TICKER,
            price=50_000.0,
            received_at=t0 + timedelta(seconds=1),
        )
    )

    snapshots = store.list_snapshots()
    topics = [s.topic for s in snapshots]
    assert topics == ["BTC-USD", "SOL-USD"]
    assert len(snapshots) == 2


def test_stale_detection_marks_old_updates() -> None:
    store = SnapshotStore(stale_threshold_seconds=30.0)
    old = datetime.now(timezone.utc) - timedelta(seconds=120)

    store.apply_event(
        MarketEvent(
            topic="BTC-USD",
            event_type=MarketEventType.TICKER,
            price=99_000.0,
            received_at=old,
        )
    )

    snap = store.get_snapshot("BTC-USD")
    assert snap is not None
    assert snap.stale is True


def test_unknown_topic_get_snapshot_none_and_list_empty() -> None:
    store = SnapshotStore()
    assert store.get_snapshot("NOT-A-TOPIC") is None
    assert store.list_snapshots() == []
