"""Process-wide runtime: owns shared services and wires them together.

Import order is deliberate: this module sits above ingestion and pulls concrete
implementations together. Lower layers (registry, cache, pubsub, Coinbase client)
must not import :mod:`app.runtime`, which avoids circular imports.
"""

from __future__ import annotations

from app.cache.snapshot_store import SnapshotStore
from app.config import Settings, get_settings
from app.ingestion.coinbase_client import CoinbaseClient
from app.pubsub.broker import PubSubBroker
from app.registry.connection_registry import ConnectionRegistry

_runtime: Runtime | None = None


class Runtime:
    """Shared hub services for one process.

    Construction order: settings → registry → snapshots → broker → Coinbase
    client (the client needs settings, broker, and snapshot store).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings if settings is not None else get_settings()
        self.connection_registry = ConnectionRegistry()
        self.snapshot_store = SnapshotStore(
            stale_threshold_seconds=self.settings.stale_threshold_seconds,
        )
        self.broker = PubSubBroker(max_queue_size=self.settings.queue_size)
        self.coinbase_client = CoinbaseClient(
            self.settings,
            self.broker,
            self.snapshot_store,
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
