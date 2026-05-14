"""MCP HTTP+SSE mounted on the FastAPI app (same Runtime as REST/WebSocket)."""

from __future__ import annotations

from starlette.routing import Mount

from app.main import app


def test_mcp_sse_and_message_mount_registered() -> None:
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/mcp/sse" in paths
    mount_paths = [r.path for r in app.routes if isinstance(r, Mount)]
    assert any(p.rstrip("/") == "/mcp/messages" for p in mount_paths)


def test_root_lists_mcp_sse_path() -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        body = client.get("/").json()
    assert body.get("mcp_sse") == "/mcp/sse"


def test_hub_metrics_snapshot_shape() -> None:
    from app.runtime import HubMetrics

    m = HubMetrics()
    snap = m.snapshot()
    assert snap["cumulative_since_process_start"] is True
    assert "process_uptime_seconds" in snap
    for key in (
        "upstream_websocket_messages_received",
        "upstream_normalized_market_events",
        "broker_queue_puts",
        "broker_drop_oldest_total",
        "downstream_ws_stream_messages_sent",
    ):
        assert key in snap
        assert snap[key] == 0


def test_downstream_ws_counter_increments() -> None:
    from app.runtime import HubMetrics

    m = HubMetrics()
    m.note_downstream_ws_stream_message()
    m.note_downstream_ws_stream_message()
    assert m.snapshot()["downstream_ws_stream_messages_sent"] == 2


def test_status_endpoint_includes_metrics() -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        body = client.get("/status").json()
    assert "metrics" in body
    m = body["metrics"]
    assert m.get("cumulative_since_process_start") is True
    uptime = m.get("process_uptime_seconds")
    assert isinstance(uptime, (int, float))
