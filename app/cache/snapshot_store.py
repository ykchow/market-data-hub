"""Latest per-topic market state for fast reads (REST, MCP, operators).

The store is **in-memory only** (no database). Ingestion calls
``apply_event`` with normalized :class:`~app.models.market_data.MarketEvent`
instances; readers use ``get_snapshot`` / ``list_snapshots``. Staleness is
derived at read time from ``updated_at`` and a configured age threshold so
callers can treat old bid/ask/trade data explicitly.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models.market_data import MarketEvent, TopicSnapshot


@dataclass
class _TopicRow:
    """Internal mutable row for one topic (not exposed outside this module)."""

    last_trade_price: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    updated_at: datetime | None = None


class SnapshotStore:
    """In-memory materialized view per topic (last trade, top of book, mid).

    A single ``dict`` maps topic id (e.g. ``BTC-USD``) to aggregated fields.
    Updates should come from one writer path (ingestion) to keep merges
    predictable; a :class:`threading.Lock` still guards read/write so
    concurrent async tasks cannot interleave non-atomic merges.

    Args:
        stale_threshold_seconds: If ``now - updated_at`` exceeds this value
            (in seconds), snapshots are marked ``stale=True``. Must be positive;
            typically taken from ``Settings.stale_threshold_seconds``.
    """

    def __init__(self, stale_threshold_seconds: float = 30.0) -> None:
        if stale_threshold_seconds <= 0:
            raise ValueError("stale_threshold_seconds must be positive")
        self._stale_threshold_seconds = stale_threshold_seconds
        self._topics: dict[str, _TopicRow] = {}
        self._lock = threading.Lock()

    def apply_event(self, event: MarketEvent) -> None:
        """Merge a normalized market event into the snapshot for ``event.topic``.

        - ``price`` updates ``last_trade_price`` when present (trade or ticker).
        - ``best_bid`` / ``best_ask`` overwrite when present on the event.
        - ``updated_at`` advances to the latest ``received_at`` seen for the row
          (handles out-of-order delivery by wall clock at the hub).

        Does not remove topics when upstream unsubscribes; last known values may
        remain until process restart (see architecture docs).
        """
        with self._lock:
            row = self._topics.get(event.topic)
            if row is None:
                row = _TopicRow()
                self._topics[event.topic] = row

            if event.price is not None:
                row.last_trade_price = event.price
            if event.best_bid is not None:
                row.best_bid = event.best_bid
            if event.best_ask is not None:
                row.best_ask = event.best_ask

            recv = event.received_at
            if recv.tzinfo is None:
                recv = recv.replace(tzinfo=timezone.utc)
            if row.updated_at is None:
                row.updated_at = recv
            else:
                row.updated_at = max(row.updated_at, recv)

    def get_snapshot(self, topic: str) -> TopicSnapshot | None:
        """Return the latest :class:`TopicSnapshot` for ``topic``, or ``None``.

        ``None`` means the hub has not yet recorded any normalized event for
        that topic. When a row exists, ``stale`` is computed from ``updated_at``
        and :attr:`stale_threshold_seconds` using UTC "now".
        """
        with self._lock:
            row = self._topics.get(topic)
            if row is None:
                return None
            return self._row_to_snapshot(topic, row)

    def list_snapshots(self) -> list[TopicSnapshot]:
        """Return snapshots for all topics that have at least one update.

        Sorted by topic name for stable, testable ordering. Each item uses the
        same staleness rules as :meth:`get_snapshot`.
        """
        with self._lock:
            items = sorted(self._topics.items(), key=lambda kv: kv[0])
            return [self._row_to_snapshot(t, r) for t, r in items]

    def _age_seconds(self, updated_at: datetime | None) -> float | None:
        """Return seconds since ``updated_at`` in UTC, or ``None`` if unknown."""
        if updated_at is None:
            return None
        now = datetime.now(timezone.utc)
        return (now - updated_at).total_seconds()

    def _is_stale(self, updated_at: datetime | None) -> bool:
        """True when there is no update time or age exceeds the threshold."""
        age = self._age_seconds(updated_at)
        if age is None:
            return True
        return age > self._stale_threshold_seconds

    def _mid_price(self, bid: float | None, ask: float | None) -> float | None:
        """Mid only when both sides of the book are known."""
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    def _row_to_snapshot(self, topic: str, row: _TopicRow) -> TopicSnapshot:
        """Build a Pydantic snapshot with fresh ``stale`` flag (caller holds lock)."""
        stale = self._is_stale(row.updated_at)
        return TopicSnapshot(
            topic=topic,
            last_trade_price=row.last_trade_price,
            best_bid=row.best_bid,
            best_ask=row.best_ask,
            mid_price=self._mid_price(row.best_bid, row.best_ask),
            updated_at=row.updated_at,
            stale=stale,
        )
