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
