"""MCP stdio server entrypoint for the market data hub.

This module is a **thin transport adapter**: it registers hub tools from
:mod:`app.mcp.tools` with the official MCP Python server and forwards
``tools/call`` to :func:`app.mcp.tools.handle_tool`. All discovery, validation,
and hub I/O live in ``tools.py`` and shared services — nothing business-specific
is duplicated here.

-------------------------------------------------------------------------------
How to run (local)
-------------------------------------------------------------------------------

1. Install the MCP SDK (not listed in the minimal app ``requirements.txt`` yet)::

       pip install mcp

2. From the repository root, with the same ``PYTHONPATH`` / venv you use for the
   FastAPI app, start the server over stdio (blocks until the client closes stdin)::

       python -m app.mcp.server

   This boots :class:`app.runtime.Runtime` (Coinbase client, registry, broker,
   snapshots) the same way as ``app.main`` so tool results match the running hub.

-------------------------------------------------------------------------------
How to attach from Cursor / Claude Desktop (example)
-------------------------------------------------------------------------------

Add an MCP server entry whose command runs this module from your project root,
for example::

    {
      "mcpServers": {
        "market-data-hub": {
          "command": "python",
          "args": ["-m", "app.mcp.server"],
          "cwd": "C:/Users/You/Projects/market-data-hub-testing"
        }
      }
    }

Use your real ``cwd`` and the ``python`` that has ``mcp`` and this package
installed. The host spawns the process and speaks JSON-RPC over stdio; you do
not open an HTTP port for this transport.

-------------------------------------------------------------------------------
Notes
-------------------------------------------------------------------------------

- For live HTTP/WebSocket traffic in parallel, run ``python -m app.main`` (or
  uvicorn) in a **separate** terminal; the MCP process is its own process with
  its own in-memory state unless you later design a remote bridge.
- Tool listings and schemas come from :func:`app.mcp.tools.list_tool_specs`;
  invocation is :func:`app.mcp.tools.handle_tool` with
  :func:`app.runtime.get_runtime`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp import types
from mcp.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from app.mcp.tools import handle_tool, list_tool_specs
from app.runtime import Runtime, get_runtime, set_runtime

logger = logging.getLogger(__name__)


def create_mcp_server() -> Server[dict[str, Any], Any]:
    """Build an MCP :class:`~mcp.server.Server` with hub tools registered."""

    server = Server(
        "market-data-hub",
        version="0.0.0",
        instructions=(
            "Real-time crypto market data hub (Coinbase). Use list_available_topics "
            "first, then describe_topic_schema / get_topic_snapshot. Streaming is "
            "documented via subscribe_to_topic_stream (WebSocket path)."
        ),
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=spec["name"],
                description=spec["description"],
                inputSchema=spec["inputSchema"],
            )
            for spec in list_tool_specs()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        runtime = get_runtime()
        args: dict[str, object] = dict(arguments or ())
        result = await handle_tool(runtime, name, args)
        text = json.dumps(result, indent=2)
        is_error = result.get("ok") is False
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            isError=is_error,
        )

    return server


async def _run_stdio() -> None:
    """Start hub runtime, serve MCP over stdio, then shut down cleanly."""
    runtime = Runtime()
    set_runtime(runtime)
    await runtime.startup()
    try:
        mcp_server = create_mcp_server()
        async with stdio_server() as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(
                    notification_options=NotificationOptions(),
                ),
            )
    finally:
        await runtime.shutdown()
        set_runtime(None)


def main() -> None:
    """CLI entry: ``python -m app.mcp.server``."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
