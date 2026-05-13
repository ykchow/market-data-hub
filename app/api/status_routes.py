"""Operational HTTP routes for health checks, hub status, topics, and snapshots.

These endpoints are intended for humans, monitors, and agents that need a simple
JSON view of in-process state. They read shared services from :class:`app.runtime.Runtime`
and do **not** mutate subscriptions or upstream demand.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from app.runtime import Runtime, get_runtime

router = APIRouter(tags=["status"])


def require_runtime() -> Runtime:
    """Resolve the process-wide :class:`Runtime` or respond with HTTP 503.

    The global runtime is registered during application startup via
    :func:`app.runtime.set_runtime`. Until then, endpoints that need registry,
    snapshot, or ingestion state cannot run.

    Returns:
        The active :class:`Runtime` singleton.

    Raises:
        HTTPException: With status 503 when the runtime has not been wired yet.
    """
    try:
        return get_runtime()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Runtime is not initialized; call set_runtime() during application startup"
            ),
        ) from exc


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe: confirms the HTTP process can answer requests.

    Returns HTTP 200 whenever this handler runs. A ``runtime`` field reports
    whether :func:`app.runtime.get_runtime` is ready so operators can distinguish
    "process up" from "hub services wired" without failing naive health checks
    that only assert a 2xx status code.

    Returns:
        JSON object with at least ``status: "ok"`` and a ``runtime`` readiness hint.
    """
    body: dict[str, Any] = {"status": "ok"}
    try:
        get_runtime()
        body["runtime"] = "ready"
    except RuntimeError:
        body["runtime"] = "not_initialized"
    return body


@router.get("/status")
async def hub_status(runtime: Annotated[Runtime, Depends(require_runtime)]) -> dict[str, Any]:
    """Rich operational snapshot: downstream registry, snapshots, and upstream topics.

    Pulls async metrics from :meth:`app.registry.connection_registry.ConnectionRegistry.get_status`,
    summarizes :class:`app.cache.snapshot_store.SnapshotStore` rows, and lists the
    Coinbase client's desired upstream product set (see
    :attr:`app.ingestion.coinbase_client.CoinbaseClient.upstream_topics`).

    Returns:
        Nested JSON suitable for dashboards and ``curl`` debugging.
    """
    registry_status = await runtime.connection_registry.get_status()
    snapshots = runtime.snapshot_store.list_snapshots()
    upstream_topics = sorted(runtime.coinbase_client.upstream_topics)
    stale_topics = [s.topic for s in snapshots if s.stale]

    return {
        "registry": registry_status,
        "snapshots": {
            "topic_count": len(snapshots),
            "stale_topic_count": len(stale_topics),
            "stale_topics": stale_topics,
        },
        "upstream": {
            "coinbase_desired_topics": upstream_topics,
            "coinbase_desired_topic_count": len(upstream_topics),
        },
    }


@router.get("/topics")
async def list_topics(runtime: Annotated[Runtime, Depends(require_runtime)]) -> dict[str, Any]:
    """Topic catalog with subscription and snapshot hints where available.

    Union of topic names seen in the connection registry (reference counts),
    the snapshot store (any row with at least one applied event), and the
    ingestion client's desired upstream set. Per-topic fields expose refcount
    (downstream demand), whether the topic is in the upstream desired set, and
    a compact snapshot freshness summary when a row exists.

    Returns:
        JSON with ``topics``: a list of per-topic status objects, sorted by name.
    """
    registry_status = await runtime.connection_registry.get_status()
    refcounts: dict[str, int] = dict(registry_status["topic_reference_counts"])
    snap_by_topic = {s.topic: s for s in runtime.snapshot_store.list_snapshots()}
    upstream_set = runtime.coinbase_client.upstream_topics

    all_names = set(refcounts) | set(snap_by_topic) | set(upstream_set)
    topics_out: list[dict[str, Any]] = []

    for name in sorted(all_names):
        snap = snap_by_topic.get(name)
        rc = refcounts.get(name, 0)
        entry: dict[str, Any] = {
            "topic": name,
            "registry_refcount": rc,
            "active_downstream_demand": rc > 0,
            "upstream_desired": name in upstream_set,
        }
        if snap is not None:
            entry["snapshot"] = {
                "stale": snap.stale,
                "updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
            }
        else:
            entry["snapshot"] = None
        topics_out.append(entry)

    return {"topics": topics_out, "topic_count": len(topics_out)}


@router.get("/snapshots/{topic}")
async def snapshot_for_topic(
    topic: str,
    runtime: Annotated[Runtime, Depends(require_runtime)],
) -> dict[str, Any]:
    """Return the latest materialized snapshot for one topic plus subscription context.

    Reads :meth:`app.cache.snapshot_store.SnapshotStore.get_snapshot` and enriches
    the payload with the registry refcount for that topic (downstream subscription
    demand) and whether the ingestion layer still lists the product in its
    desired upstream set.

    Args:
        topic: Product id (e.g. ``BTC-USD``).

    Returns:
        JSON including ``snapshot`` (Pydantic dump with JSON-friendly datetimes)
        and light connection/subscription metadata.

    Raises:
        HTTPException: 404 when no snapshot row exists yet for ``topic``.
    """
    registry_status = await runtime.connection_registry.get_status()
    refcounts: dict[str, int] = dict(registry_status["topic_reference_counts"])
    snap = runtime.snapshot_store.get_snapshot(topic)
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail=f"No snapshot data for topic {topic!r}",
        )

    return {
        "topic": topic,
        "registry_refcount": refcounts.get(topic, 0),
        "active_downstream_demand": refcounts.get(topic, 0) > 0,
        "upstream_desired": topic in runtime.coinbase_client.upstream_topics,
        "snapshot": snap.model_dump(mode="json"),
    }
