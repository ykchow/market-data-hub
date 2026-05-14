"""MCP tool definitions and handlers for LLM agents.

This module is the **application-side contract** for Model Context Protocol tools:
``list_tool_specs`` returns metadata (names, descriptions, JSON Schema arguments)
for registration with an MCP host, and ``handle_tool`` dispatches invocations into
read-only hub services (registry, snapshot store, settings).

**Intended MCP usage**

- Host process loads :func:`list_tool_specs` during capability negotiation. For the **same**
  in-memory hub as REST/WebSocket, attach over **SSE** to the running FastAPI app
  (``GET /mcp/sse`` on uvicorn; see :mod:`app.mcp.http_mcp`). For stdio-only hosts,
  ``python -m app.mcp.server`` uses a **separate** ``Runtime`` in that process.
- On ``tools/call``, the host parses JSON arguments, resolves the shared
  :class:`~app.runtime.Runtime` singleton (same instance as FastAPI when using SSE),
  and awaits :func:`handle_tool` so registry metrics stay consistent with REST.
- Agents should **list topics and schema first**, then **read snapshots**; live
  streaming is not implemented over MCP in this module—see
  :func:`subscribe_to_topic_stream` for the supported WebSocket-based path.

Responses are plain JSON-serializable dicts with explicit ``ok``, ``error_code``,
and ``detail`` fields where failures must not be silent.
"""

from __future__ import annotations

import re
from typing import Any, Final

from app.models.market_data import MarketEvent, MarketEventType, TopicSnapshot
from app.runtime import Runtime

# Coinbase-style product id: BASE-QUOTE (aligned with ``app.main`` WebSocket gate).
_TOPIC_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z0-9]{1,24}-[A-Z0-9]{1,24}$")

# LLM/host-facing: same rule as ``_TOPIC_PATTERN`` (see docs/MCP_CONTEXT.md §5.5).
_TOPIC_ID_ARG_DESCRIPTION: Final[str] = (
    "BASE-QUOTE product id: ASCII uppercase letters and digits only, exactly one hyphen, "
    "1-24 characters per segment (e.g. BTC-USD, ETH-USD). "
    "Pattern ^[A-Z0-9]{1,24}-[A-Z0-9]{1,24}$."
)

_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "list_available_topics",
        "describe_topic_schema",
        "get_topic_snapshot",
        "subscribe_to_topic_stream",
    }
)


def _valid_topic_id(topic: str) -> bool:
    return bool(_TOPIC_PATTERN.fullmatch(topic))


def _tool_error(
    *,
    error_code: str,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": False,
        "error_code": error_code,
        "detail": detail,
    }
    if extra:
        body.update(extra)
    return body


def _tool_ok(payload: dict[str, Any]) -> dict[str, Any]:
    out = {"ok": True, "error_code": None, "detail": None}
    out.update(payload)
    return out


def list_tool_specs() -> list[dict[str, Any]]:
    """Return MCP tool metadata for host registration (LLM-facing descriptions).

    Each item matches common MCP ``Tool`` shape: ``name``, ``description``, and
    ``inputSchema`` (JSON Schema draft style) so hosts can validate arguments
    before invoking handlers.
    """
    return [
        {
            "name": "list_available_topics",
            "description": (
                "Discover which market topics this hub knows about: configured defaults, "
                "upstream Coinbase subscription interest, in-memory snapshots, and downstream "
                "registry demand. Call this first before querying prices so you use real topic "
                "ids (e.g. BTC-USD). "
                "Pass arguments as an empty JSON object {}; any property yields invalid_arguments."
            ),
            "inputSchema": {
                "type": "object",
                "description": "Must be empty (no properties).",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "describe_topic_schema",
            "description": (
                "Return the canonical JSON field definitions for live events and materialized "
                "snapshots for one product id. Use after listing topics to interpret "
                "get_topic_snapshot output and WebSocket stream payloads without guessing types."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": _TOPIC_ID_ARG_DESCRIPTION,
                    },
                },
                "required": ["topic"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_topic_snapshot",
            "description": (
                "Read the latest in-memory bid/ask/trade/mid snapshot for one topic plus light "
                "subscription context (registry refcount, upstream desired flag). Respects "
                "staleness: check snapshot.stale and timestamps before treating numbers as live."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": _TOPIC_ID_ARG_DESCRIPTION,
                    },
                },
                "required": ["topic"],
                "additionalProperties": False,
            },
        },
        {
            "name": "subscribe_to_topic_stream",
            "description": (
                "Does not stream live events over MCP. Returns JSON with WebSocket path (/ws), "
                "handshake note, subscribe_example, and after_close: subscriptions are per socket—"
                "after any /ws close the client must reconnect and subscribe again; no replay."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": _TOPIC_ID_ARG_DESCRIPTION,
                    },
                },
                "required": ["topic"],
                "additionalProperties": False,
            },
        },
    ]


async def list_available_topics(runtime: Runtime) -> dict[str, Any]:
    """Aggregate topic ids from settings, registry, snapshots, and upstream client."""
    registry_status = await runtime.connection_registry.get_status()
    refcounts: dict[str, int] = {
        str(k): int(v) for k, v in dict(registry_status["topic_reference_counts"]).items()
    }
    snap_by_topic = {s.topic: s for s in runtime.snapshot_store.list_snapshots()}
    upstream_set = runtime.coinbase_client.upstream_topics
    defaults = list(runtime.settings.default_topics)

    all_names = set(refcounts) | set(snap_by_topic) | set(upstream_set) | set(defaults)
    topics_out: list[dict[str, Any]] = []

    for name in sorted(all_names):
        snap = snap_by_topic.get(name)
        rc = refcounts.get(name, 0)
        entry: dict[str, Any] = {
            "topic": name,
            "registry_refcount": rc,
            "active_downstream_demand": rc > 0,
            "upstream_desired": name in upstream_set,
            "in_default_catalog": name in set(defaults),
        }
        if snap is not None:
            entry["snapshot"] = {
                "stale": snap.stale,
                "updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
            }
        else:
            entry["snapshot"] = None
        topics_out.append(entry)

    return _tool_ok(
        {
            "topics": topics_out,
            "topic_count": len(topics_out),
            "default_topics": defaults,
        }
    )


def describe_topic_schema(runtime: Runtime, topic: str) -> dict[str, Any]:
    """Document canonical event and snapshot shapes for one topic id (no I/O)."""
    if not _valid_topic_id(topic):
        return _tool_error(
            error_code="invalid_topic",
            detail=(
                "Topic must match BASE-QUOTE product id (letters/digits, one hyphen), "
                "e.g. BTC-USD."
            ),
            extra={"topic": topic},
        )

    event_schema = MarketEvent.model_json_schema()
    snapshot_schema = TopicSnapshot.model_json_schema()
    return _tool_ok(
        {
            "topic": topic,
            "hub_snapshot_policy": {
                "stale_threshold_seconds": runtime.settings.stale_threshold_seconds,
                "queue_size": runtime.settings.queue_size,
            },
            "canonical_event": {
                "model": "MarketEvent",
                "description": (
                    "Normalized hub event produced from Coinbase wire data; WebSocket "
                    "stream items use the same field names."
                ),
                "json_schema": event_schema,
                "event_type_enum": [e.value for e in MarketEventType],
            },
            "topic_snapshot": {
                "model": "TopicSnapshot",
                "description": (
                    "Materialized per-topic view returned by get_topic_snapshot; stale flag "
                    "is derived from last update age vs configured threshold."
                ),
                "json_schema": snapshot_schema,
            },
        }
    )


async def get_topic_snapshot(runtime: Runtime, topic: str) -> dict[str, Any]:
    """Return latest TopicSnapshot plus refcount/upstream hints, or a clear error."""
    if not _valid_topic_id(topic):
        return _tool_error(
            error_code="invalid_topic",
            detail=(
                "Topic must match BASE-QUOTE product id (letters/digits, one hyphen), "
                "e.g. BTC-USD."
            ),
            extra={"topic": topic},
        )

    registry_status = await runtime.connection_registry.get_status()
    refcounts: dict[str, int] = {
        str(k): int(v) for k, v in dict(registry_status["topic_reference_counts"]).items()
    }
    snap = runtime.snapshot_store.get_snapshot(topic)
    if snap is None:
        return _tool_error(
            error_code="no_snapshot_yet",
            detail=(
                f"No snapshot row exists for topic {topic!r}. The id may be valid but the hub "
                "has not ingested a normalized event for it yet, or the product is unsupported "
                "upstream."
            ),
            extra={
                "topic": topic,
                "registry_refcount": refcounts.get(topic, 0),
                "active_downstream_demand": refcounts.get(topic, 0) > 0,
                "upstream_desired": topic in runtime.coinbase_client.upstream_topics,
            },
        )

    return _tool_ok(
        {
            "topic": topic,
            "registry_refcount": refcounts.get(topic, 0),
            "active_downstream_demand": refcounts.get(topic, 0) > 0,
            "upstream_desired": topic in runtime.coinbase_client.upstream_topics,
            "snapshot": snap.model_dump(mode="json"),
        }
    )


def subscribe_to_topic_stream(runtime: Runtime, topic: str) -> dict[str, Any]:
    """Placeholder until MCP streaming is wired to PubSubBroker consumer queues."""
    if not _valid_topic_id(topic):
        return _tool_error(
            error_code="invalid_topic",
            detail=(
                "Topic must match BASE-QUOTE product id (letters/digits, one hyphen), "
                "e.g. BTC-USD."
            ),
            extra={"topic": topic},
        )

    return _tool_ok(
        {
            "topic": topic,
            "queue_size_hint": runtime.settings.queue_size,
            "streaming_via_mcp": False,
            "message": (
                "Live multiplexed streaming is not implemented on this MCP surface yet. "
                "Use the hub WebSocket endpoint with a subscribe control message instead."
            ),
            "websocket": {
                "path": "/ws",
                "handshake": "Standard FastAPI WebSocket upgrade on the same base URL as HTTP.",
                "subscribe_example": {"op": "subscribe", "topic": topic},
                "event_shape": "Same fields as MarketEvent (see describe_topic_schema).",
                "after_close": (
                    "Subscriptions are per WebSocket. After any close, open a new connection "
                    "to /ws and send subscribe again for each topic; missed MarketEvents are not "
                    "replayed."
                ),
            },
        }
    )


def _require_str(arguments: dict[str, object], key: str) -> str | None:
    raw = arguments.get(key)
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    return None


async def handle_tool(
    runtime: Runtime,
    name: str,
    arguments: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Dispatch MCP ``tools/call`` by name into hub services.

    Args:
        runtime: Shared process :class:`~app.runtime.Runtime` (must match FastAPI).
        name: Tool name from :func:`list_tool_specs`.
        arguments: Parsed JSON object from the MCP client; may be empty for list tools.

    Returns:
        A structured dict; failures use ``ok: false`` with ``error_code`` and ``detail``.
    """
    args = dict(arguments or ())
    if name not in _TOOL_NAMES:
        return _tool_error(
            error_code="unknown_tool",
            detail=f"No MCP tool named {name!r}. Valid tools: {', '.join(sorted(_TOOL_NAMES))}.",
            extra={"requested_name": name},
        )

    if name == "list_available_topics":
        if args:
            return _tool_error(
                error_code="invalid_arguments",
                detail="list_available_topics does not accept arguments; pass an empty object.",
            )
        return await list_available_topics(runtime)

    if name == "describe_topic_schema":
        topic = _require_str(args, "topic")
        if topic is None:
            return _tool_error(
                error_code="invalid_arguments",
                detail="Missing or non-string 'topic' argument.",
            )
        return describe_topic_schema(runtime, topic)

    if name == "get_topic_snapshot":
        topic = _require_str(args, "topic")
        if topic is None:
            return _tool_error(
                error_code="invalid_arguments",
                detail="Missing or non-string 'topic' argument.",
            )
        return await get_topic_snapshot(runtime, topic)

    if name == "subscribe_to_topic_stream":
        topic = _require_str(args, "topic")
        if topic is None:
            return _tool_error(
                error_code="invalid_arguments",
                detail="Missing or non-string 'topic' argument.",
            )
        return subscribe_to_topic_stream(runtime, topic)

    return _tool_error(
        error_code="internal_error",
        detail="Tool dispatch fell through unexpectedly.",
        extra={"requested_name": name},
    )
