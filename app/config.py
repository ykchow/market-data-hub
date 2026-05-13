"""Application configuration loaded from environment variables and optional `.env` file.

Values are used across ingestion (Coinbase WebSocket), pub/sub queue bounds,
snapshot staleness, reconnect tuning, and logging. See ``Settings`` for field
documentation and override names (uppercase env keys).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_TOPICS: tuple[str, ...] = ("BTC-USD", "ETH-USD", "SOL-USD")


class Settings(BaseSettings):
    """Runtime settings for the market data hub.

    Environment variables use the same name as fields in UPPER_SNAKE_CASE
    (for example ``COINBASE_WS_URL``, ``DEFAULT_TOPICS``). Optional ``.env`` in
    the process working directory is read on instantiation.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    coinbase_ws_url: str = Field(
        default="wss://ws-feed.exchange.coinbase.com",
        description="Coinbase Exchange WebSocket endpoint for market data.",
    )
    default_topics: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_TOPICS),
        description=(
            "Product IDs to treat as defaults (e.g. warm subscriptions or docs). "
            "Override with JSON array in env, e.g. DEFAULT_TOPICS='[\"BTC-USD\"]'."
        ),
    )
    queue_size: int = Field(
        default=1_000,
        ge=1,
        description="Max items per downstream consumer asyncio.Queue (backpressure bound).",
    )
    stale_threshold_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Age after last fresh data beyond which a topic snapshot is considered stale.",
    )
    reconnect_delay_seconds: float = Field(
        default=1.0,
        gt=0,
        description="Base delay (seconds) before retrying an upstream WebSocket reconnect.",
    )
    log_level: str = Field(
        default="INFO",
        description="Logging level name (e.g. DEBUG, INFO, WARNING).",
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance (one per process).

    Use this from FastAPI dependencies or runtime wiring so configuration is
    parsed once and stays consistent for the app lifetime.
    """
    return Settings()
