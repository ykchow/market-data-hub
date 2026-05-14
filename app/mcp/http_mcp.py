"""MCP over HTTP + SSE on the same uvicorn process as FastAPI (shared :class:`~app.runtime.Runtime`).

Uses :class:`mcp.server.sse.SseServerTransport` so MCP clients that support an SSE URL
attach to the live hub instead of spawning ``python -m app.mcp.server`` (a second process
with its own ``Runtime``).

Endpoints (default mount, no API prefix):

- ``GET /mcp/sse`` — establish the SSE session (MCP host "SSE URL" points here).
- ``POST /mcp/messages/?session_id=…`` — client JSON-RPC (handled by the transport; do not mount elsewhere).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from mcp.server import NotificationOptions
from mcp.server.sse import SseServerTransport
from starlette.responses import Response

from app.mcp.server import create_mcp_server

logger = logging.getLogger(__name__)

# POST path must match SseServerTransport's endpoint string (used in the SSE "endpoint" event).
MCP_MESSAGES_PATH = "/mcp/messages/"
MCP_SSE_PATH = "/mcp/sse"

_sse_singleton: SseServerTransport | None = None


def _get_sse_transport() -> SseServerTransport:
    global _sse_singleton
    if _sse_singleton is None:
        _sse_singleton = SseServerTransport(MCP_MESSAGES_PATH)
    return _sse_singleton


def install_mcp_http(app: FastAPI) -> None:
    """Register MCP SSE + message POST routes on ``app`` (safe to call once per app instance)."""
    if getattr(app.state, "mcp_http_installed", False):
        return
    app.state.mcp_http_installed = True

    sse = _get_sse_transport()

    @app.get(MCP_SSE_PATH)
    async def mcp_sse(request: Request) -> Response:
        logger.debug("MCP SSE session starting")
        async with sse.connect_sse(
            request.scope,
            request.receive,
            request._send,
        ) as streams:
            mcp = create_mcp_server()
            await mcp.run(
                streams[0],
                streams[1],
                mcp.create_initialization_options(
                    notification_options=NotificationOptions(),
                ),
            )
        return Response()

    app.mount(MCP_MESSAGES_PATH, sse.handle_post_message)
