"""Coinbase Exchange WebSocket ingestion: subscribe, parse, normalize, fan-out.

Connects to Coinbase's public market-data feed, keeps the upstream subscription set
in sync with hub demand, turns wire JSON into :class:`~app.models.market_data.MarketEvent`,
updates :class:`~app.cache.snapshot_store.SnapshotStore`, and publishes on
:class:`~app.pubsub.broker.PubSubBroker`. Malformed frames are skipped with a log line;
transport errors trigger a capped exponential backoff reconnect and a full
re-subscribe of the current topic set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.protocol import State

from app.cache.snapshot_store import SnapshotStore
from app.config import Settings
from app.models.market_data import MarketEvent, MarketEventType
from app.pubsub.broker import PubSubBroker

logger = logging.getLogger(__name__)

# Channels that give top-of-book + last trade without full L2 complexity.
_DEFAULT_CHANNELS: tuple[str, ...] = ("ticker", "matches")

# Cap backoff so a long outage does not sleep unbounded between tries.
_MAX_BACKOFF_SECONDS = 60.0


def _ws_is_open(ws: ClientConnection | None) -> bool:
    """True when the client socket can still send (websockets v11 ``closed`` vs v16 ``state``)."""
    if ws is None:
        return False
    state = getattr(ws, "state", None)
    if state is not None:
        return state is State.OPEN
    return not bool(getattr(ws, "closed", False))


class CoinbaseClient:
    """Single upstream WebSocket to Coinbase; normalizes events for the rest of the hub.

    Lifecycle:
        1. ``await start()`` — begins a background task that connects, reads messages,
           reconnects on failure, and re-sends subscriptions after each connect.
        2. ``await subscribe_topic`` / ``await unsubscribe_topic`` — maintain the set of
           product IDs (topics) subscribed upstream. Calls are safe from any asyncio task.
        3. ``await stop()`` — signals shutdown, cancels the background task, and closes
           the socket.

    Inbound messages are parsed on the reader task; each normalized event updates the
    snapshot store and is published to the broker for the same ``topic`` (product id).
    """

    def __init__(
        self,
        settings: Settings,
        broker: PubSubBroker,
        snapshot_store: SnapshotStore,
        *,
        channels: tuple[str, ...] = _DEFAULT_CHANNELS,
    ) -> None:
        self._settings = settings
        self._broker = broker
        self._snapshot_store = snapshot_store
        self._channels = channels

        self._lock = asyncio.Lock()
        # Product IDs we should be subscribed to on the wire (hub demand).
        self._topics: set[str] = set()

        self._stop = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._ws: ClientConnection | None = None
        # Serializes sends and coordinates clearing ``_ws`` when the connection ends.
        self._ws_lock = asyncio.Lock()

    @property
    def upstream_topics(self) -> frozenset[str]:
        """Copy of topics currently requested for upstream subscription."""
        return frozenset(self._topics)

    async def start(self) -> None:
        """Start the reconnect loop (idempotent: second call does nothing)."""
        async with self._lock:
            if self._runner is not None and not self._runner.done():
                return
            self._stop.clear()
            self._runner = asyncio.create_task(self._run_loop(), name="coinbase-ws-runner")

    async def stop(self) -> None:
        """Stop the reconnect loop and close any open WebSocket."""
        self._stop.set()
        runner = self._runner
        self._runner = None
        if runner is not None:
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
        await self._close_ws()

    async def subscribe_topic(self, topic: str) -> None:
        """Add ``topic`` (e.g. ``BTC-USD``) to the upstream set and subscribe if connected."""
        async with self._lock:
            self._topics.add(topic)
        await self._send_subscribe([topic])

    async def unsubscribe_topic(self, topic: str) -> None:
        """Remove ``topic`` from the upstream set and unsubscribe if connected."""
        async with self._lock:
            self._topics.discard(topic)
        await self._send_unsubscribe([topic])

    async def _close_ws(self) -> None:
        async with self._ws_lock:
            ws = self._ws
            self._ws = None
        if ws is not None and _ws_is_open(ws):
            await ws.close()

    async def _set_ws(self, ws: ClientConnection | None) -> None:
        async with self._ws_lock:
            self._ws = ws

    async def _run_loop(self) -> None:
        """Connect with backoff until ``stop``; re-subscribe after every successful connect."""
        attempt = 0
        base = self._settings.reconnect_delay_seconds

        while not self._stop.is_set():
            ws: ClientConnection | None = None
            try:
                logger.info("Connecting to Coinbase WebSocket %s", self._settings.coinbase_ws_url)
                ws = await websockets.connect(
                    self._settings.coinbase_ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                )
                await self._set_ws(ws)
                attempt = 0
                logger.info("Coinbase WebSocket connected")
                await self._wire_subscribe_all()
                await self._receive_until_closed(ws)
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                logger.warning(
                    "Coinbase WebSocket closed: code=%s reason=%s",
                    exc.code,
                    exc.reason,
                )
            except (WebSocketException, OSError, TimeoutError) as exc:
                logger.warning("Coinbase WebSocket error: %s", exc, exc_info=logger.isEnabledFor(logging.DEBUG))
            finally:
                await self._set_ws(None)
                if ws is not None and _ws_is_open(ws):
                    await ws.close()

            if self._stop.is_set():
                break

            attempt += 1
            raw_delay = min(base * (2 ** min(attempt, 8)), _MAX_BACKOFF_SECONDS)
            jitter = random.uniform(0.8, 1.2)
            delay = min(raw_delay * jitter, _MAX_BACKOFF_SECONDS)
            logger.info("Reconnecting to Coinbase in %.2fs (attempt %s)", delay, attempt)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def _wire_subscribe_all(self) -> None:
        """Send subscribe for every topic in the active set (e.g. after connect)."""
        async with self._lock:
            topics = sorted(self._topics)
        if topics:
            await self._send_subscribe(topics)

    async def _send_subscribe(self, product_ids: list[str]) -> None:
        """Subscribe ``product_ids`` on the live socket (no-op if disconnected or empty)."""
        if not product_ids:
            return
        payload: dict[str, Any] = {
            "type": "subscribe",
            "product_ids": product_ids,
            "channels": list(self._channels),
        }
        await self._send_json(payload)

    async def _send_unsubscribe(self, product_ids: list[str]) -> None:
        """Unsubscribe ``product_ids`` on the live socket (no-op if disconnected or empty)."""
        if not product_ids:
            return
        payload: dict[str, Any] = {
            "type": "unsubscribe",
            "product_ids": product_ids,
            "channels": list(self._channels),
        }
        await self._send_json(payload)

    async def _send_json(self, payload: dict[str, Any]) -> None:
        async with self._ws_lock:
            ws = self._ws
            if ws is None or not _ws_is_open(ws):
                return
            try:
                await ws.send(json.dumps(payload))
            except (ConnectionClosed, TypeError, ValueError) as exc:
                logger.warning("Failed to send to Coinbase: %s", exc)

    async def _receive_until_closed(self, ws: ClientConnection) -> None:
        """Read text frames until the connection drops or ``stop`` is set."""
        while not self._stop.is_set():
            try:
                raw = await ws.recv()
            except ConnectionClosed:
                break
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning("Coinbase sent non-UTF8 binary frame; skipping")
                    continue
            await self._dispatch_raw(raw)

    async def _dispatch_raw(self, raw: str) -> None:
        """Parse one text frame; if it becomes a :class:`MarketEvent`, update snapshot and broker."""
        event = self._parse_raw_to_event(raw)
        if event is None:
            return

        async with self._lock:
            still_wanted = event.topic in self._topics
        if not still_wanted:
            return

        try:
            self._snapshot_store.apply_event(event)
        except Exception:
            logger.exception("SnapshotStore.apply_event failed for topic=%s", event.topic)
            return

        try:
            await self._broker.publish(event.topic, event)
        except Exception:
            logger.exception("PubSubBroker.publish failed for topic=%s", event.topic)

    def _parse_raw_to_event(self, raw: str) -> MarketEvent | None:
        """Decode JSON, ignore control messages, return a normalized event or ``None``."""
        try:
            msg: Any = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Coinbase malformed JSON (length=%s); skipping", len(raw))
            return None

        if not isinstance(msg, dict):
            logger.warning("Coinbase message is not a JSON object; skipping")
            return None

        msg_type = msg.get("type")
        if msg_type == "error":
            logger.error("Coinbase error message: %s", msg)
            return None
        if msg_type in ("subscriptions", "subscription"):
            logger.debug("Coinbase subscription ack: %s", msg)
            return None
        if msg_type == "heartbeat":
            return None

        return self._normalize_coinbase_message(msg)

    def _normalize_coinbase_message(self, msg: dict[str, Any]) -> MarketEvent | None:
        """Map a Coinbase object to :class:`MarketEvent`, or ``None`` if ignored or bad."""
        msg_type = msg.get("type")
        product_id = msg.get("product_id")
        if not isinstance(product_id, str) or not product_id:
            return None

        exchange_ms = self._parse_time_to_ms(msg.get("time"))

        if msg_type == "ticker":
            return self._parse_ticker(product_id, msg, exchange_ms)
        if msg_type in ("match", "last_match"):
            return self._parse_match(product_id, msg, exchange_ms)

        return None

    def _parse_ticker(self, product_id: str, msg: dict[str, Any], exchange_ms: int | None) -> MarketEvent | None:
        price = self._optional_float(msg.get("price"))
        best_bid = self._optional_float(msg.get("best_bid"))
        best_ask = self._optional_float(msg.get("best_ask"))
        if price is None and best_bid is None and best_ask is None:
            return None
        return MarketEvent(
            topic=product_id,
            event_type=MarketEventType.TICKER,
            price=price,
            best_bid=best_bid,
            best_ask=best_ask,
            exchange_timestamp_ms=exchange_ms,
            received_at=datetime.now(timezone.utc),
        )

    def _parse_match(self, product_id: str, msg: dict[str, Any], exchange_ms: int | None) -> MarketEvent | None:
        price = self._optional_float(msg.get("price"))
        size = self._optional_float(msg.get("size"))
        if price is None:
            return None
        return MarketEvent(
            topic=product_id,
            event_type=MarketEventType.TRADE,
            price=price,
            size=size,
            exchange_timestamp_ms=exchange_ms,
            received_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_time_to_ms(time_value: Any) -> int | None:
        """Coinbase uses ISO-8601 strings in ``time``; return ms since epoch or ``None``."""
        if not isinstance(time_value, str) or not time_value:
            return None
        try:
            normalized = time_value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError, OSError):
            return None
