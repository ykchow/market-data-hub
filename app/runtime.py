"""Process-wide runtime: owns shared services and wires them together.

Import order is deliberate: this module sits above ingestion and pulls concrete
implementations together. Lower layers (registry, cache, pubsub, Coinbase client)
must not import :mod:`app.runtime`, which avoids circular imports.
"""

from __future__ import annotations

import time
from typing import Any

from app.cache.snapshot_store import SnapshotStore
from app.config import Settings, get_settings
from app.ingestion.coinbase_client import CoinbaseClient
from app.pubsub.broker import PubSubBroker
from app.registry.connection_registry import ConnectionRegistry

_runtime: Runtime | None = None


class HubMetrics:
    """Cumulative counters since construction (typically one per process).

    Mutations run on the asyncio event loop; ``snapshot`` is best-effort for
    operators computing rates as Δcount/Δtime.
    """

    def __init__(self) -> None:
        self._started_mono = time.monotonic()
        self.upstream_websocket_messages_received: int = 0
        self.upstream_normalized_market_events: int = 0
        self.broker_queue_puts: int = 0
        self.broker_drop_oldest_total: int = 0
        self.downstream_ws_stream_messages_sent: int = 0

    def note_upstream_websocket_message(self) -> None:
        self.upstream_websocket_messages_received += 1

    def note_upstream_normalized_market_event(self) -> None:
        self.upstream_normalized_market_events += 1

    def note_broker_queue_put(self, n: int = 1) -> None:
        self.broker_queue_puts += n

    def note_broker_drop_oldest(self, n: int = 1) -> None:
        self.broker_drop_oldest_total += n

    def note_downstream_ws_stream_message(self) -> None:
        self.downstream_ws_stream_messages_sent += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "cumulative_since_process_start": True,
            "process_uptime_seconds": round(time.monotonic() - self._started_mono, 3),
            "upstream_websocket_messages_received": self.upstream_websocket_messages_received,
            "upstream_normalized_market_events": self.upstream_normalized_market_events,
            "broker_queue_puts": self.broker_queue_puts,
            "broker_drop_oldest_total": self.broker_drop_oldest_total,
            "downstream_ws_stream_messages_sent": self.downstream_ws_stream_messages_sent,
        }


class Runtime:
    """Shared hub services for one process.

    Construction order: settings → ``HubMetrics`` → registry → snapshots → broker → Coinbase
    client (the client needs settings, broker, snapshot store, and metrics).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings if settings is not None else get_settings()
        self.hub_metrics = HubMetrics()
        self.connection_registry = ConnectionRegistry()
        self.snapshot_store = SnapshotStore(
            stale_threshold_seconds=self.settings.stale_threshold_seconds,
        )
        self.broker = PubSubBroker(
            max_queue_size=self.settings.queue_size,
            hub_metrics=self.hub_metrics,
        )
        self.coinbase_client = CoinbaseClient(
            self.settings,
            self.broker,
            self.snapshot_store,
            hub_metrics=self.hub_metrics,
        )

    async def startup(self) -> None:
        """Start background work (upstream WebSocket runner)."""
        await self.coinbase_client.start()

    async def shutdown(self) -> None:
        """Stop background work and release upstream resources."""
        await self.coinbase_client.stop()


def set_runtime(runtime: Runtime | None) -> None:
    """Register the global runtime (typically from FastAPI lifespan)."""
    global _runtime
    _runtime = runtime


def get_runtime() -> Runtime:
    """Return the process-wide :class:`Runtime` (for FastAPI ``Depends``)."""
    if _runtime is None:
        msg = "Runtime is not initialized; call set_runtime() during application startup"
        raise RuntimeError(msg)
    return _runtime
