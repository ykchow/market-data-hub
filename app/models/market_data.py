"""Shared Pydantic shapes for ingestion, pub/sub, snapshots, and control messages."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MarketEventType(StrEnum):
    """Kind of normalized update produced by the ingestion layer."""

    TRADE = "trade"
    TICKER = "ticker"
    L2 = "l2"


class MarketEvent(BaseModel):
    """Canonical market update after parsing venue wire format (exchange-agnostic fields)."""

    model_config = ConfigDict(use_enum_values=True)

    # Product id (e.g. BTC-USD) shared across WebSocket, REST, and MCP.
    topic: str
    # What changed: last match, consolidated ticker, or order-book style update.
    event_type: MarketEventType
    # Last trade (when applicable).
    price: float | None = None
    size: float | None = None
    # Top-of-book (ticker / L2-derived).
    best_bid: float | None = None
    best_ask: float | None = None
    # Venue-provided event time in milliseconds since Unix epoch, if present on wire.
    exchange_timestamp_ms: int | None = None
    # When the hub received or emitted this normalized event (UTC).
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TopicSnapshot(BaseModel):
    """Materialized “now” view for one topic: REST, MCP, and quick sanity checks."""

    topic: str
    last_trade_price: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    mid_price: float | None = None
    # Last time any field was updated from the stream (UTC).
    updated_at: datetime | None = None
    # True when data is too old or upstream is unavailable per hub policy.
    stale: bool = False


class SubscribeRequest(BaseModel):
    """Client asks to receive live events for a topic (WebSocket / internal control plane)."""

    op: Literal["subscribe"] = "subscribe"
    topic: str


class UnsubscribeRequest(BaseModel):
    """Client stops receiving events for a topic; registry decrements refcount."""

    op: Literal["unsubscribe"] = "unsubscribe"
    topic: str


class SubscriptionResponse(BaseModel):
    """Hub reply after a subscribe or unsubscribe attempt (explicit success or error)."""

    ok: bool
    op: Literal["subscribe", "unsubscribe"]
    topic: str
    # Machine-friendly error tag when ok is False (e.g. unknown_topic).
    error_code: str | None = None
    # Human- and agent-readable detail.
    detail: str | None = None


class ConnectionStatus(BaseModel):
    """Per-downstream-consumer view for metrics and status APIs."""

    consumer_id: str
    connected: bool
    connected_at: datetime | None = None
    # Topics this consumer is currently subscribed to.
    subscribed_topics: list[str] = Field(default_factory=list)


class TopicStatus(BaseModel):
    """Per-topic operational summary: demand, upstream interest, and freshness."""

    topic: str
    # Distinct downstream consumers with an active subscription to this topic.
    subscriber_count: int = 0
    # Whether the hub currently holds an upstream subscription for this topic.
    upstream_active: bool = False
    # Snapshot freshness flag aligned with SnapshotStore policy.
    stale: bool = False
