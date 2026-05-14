"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.api.status_routes import router as status_router
from app.mcp.http_mcp import MCP_SSE_PATH, install_mcp_http
from app.models.market_data import (
    MarketEvent,
    SubscribeRequest,
    SubscriptionResponse,
    UnsubscribeRequest,
)
from app.runtime import Runtime, get_runtime, set_runtime

logger = logging.getLogger(__name__)

# Coinbase-style product id: BASE-QUOTE (letters/digits only, one hyphen).
_TOPIC_PATTERN = re.compile(r"^[A-Z0-9]{1,24}-[A-Z0-9]{1,24}$")


def _valid_topic_id(topic: str) -> bool:
    return bool(_TOPIC_PATTERN.fullmatch(topic))


def _topic_refcount(status: dict[str, Any], topic: str) -> int:
    return int(status["topic_reference_counts"].get(topic, 0))


def _serialize_stream_item(item: object) -> dict[str, Any]:
    if isinstance(item, MarketEvent):
        return item.model_dump(mode="json")
    msg = f"unexpected stream payload type: {type(item).__name__}"
    raise TypeError(msg)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> Any:
    runtime = Runtime()
    set_runtime(runtime)
    await runtime.startup()
    try:
        yield
    finally:
        await runtime.shutdown()
        set_runtime(None)


app = FastAPI(
    title="Market Data Hub",
    version="0.0.0",
    lifespan=lifespan,
)
app.include_router(status_router)
install_mcp_http(app)


@app.get("/")
def root() -> dict[str, str]:
    """Minimal service index for operators and agents."""
    return {
        "service": "market-data-hub",
        "health": "/health",
        "status": "/status",
        "topics_ws": "/ws",
        "mcp_sse": MCP_SSE_PATH,
    }


@app.websocket("/ws")
async def topic_stream(websocket: WebSocket) -> None:
    """Stream normalized market events for subscribed topics.

    Wire protocol (JSON text frames only):

    **Client → server**

    - Subscribe to a product id (topic), e.g. Coinbase ``BTC-USD``::

        {"op": "subscribe", "topic": "BTC-USD"}

    - Remove one topic from this connection::

        {"op": "unsubscribe", "topic": "BTC-USD"}

    **Server → client**

    - After each control message, an acknowledgement object::

        {"ok": true|false, "op": "subscribe"|"unsubscribe", "topic": "<id>",
         "error_code": null | "<code>", "detail": null | "<message>"}

      For ``subscribe``, ``error_code`` may include ``invalid_topic``,
      ``unknown_consumer``, or ``upstream_subscribe_failed`` (first downstream
      demand for a topic when the hub cannot complete the Coinbase subscribe).

    - When data is available, one JSON object per normalized event, using the
      same field names as ``MarketEvent`` (``topic``, ``event_type``, ``price``,
      ``best_bid``, ``best_ask``, ``received_at``, …).

    Subscriptions are scoped to this WebSocket. On disconnect, all topics for
    this consumer are removed from the registry and broker; upstream Coinbase
    subscriptions follow global refcount (0 = no longer subscribed upstream).
    """
    await websocket.accept()
    try:
        runtime = get_runtime()
    except RuntimeError:
        await websocket.close(code=1011)
        return

    consumer_id = uuid.uuid4().hex
    queue_ready = asyncio.Event()

    async def pump_queue() -> None:
        await queue_ready.wait()
        queue = runtime.broker.get_consumer_queue(consumer_id)
        if queue is None:
            return
        while True:
            item = await queue.get()
            await websocket.send_json(_serialize_stream_item(item))

    pump_task = asyncio.create_task(pump_queue(), name=f"ws-pump-{consumer_id}")

    await runtime.connection_registry.register_consumer(consumer_id)

    try:
        while True:
            try:
                raw = await websocket.receive_json()
            except WebSocketDisconnect:
                break

            if not isinstance(raw, dict):
                await websocket.send_json(
                    {
                        "ok": False,
                        "error_code": "invalid_message",
                        "detail": "JSON object required",
                    }
                )
                continue

            op = raw.get("op")
            if op == "subscribe":
                await _handle_subscribe(websocket, runtime, consumer_id, raw, queue_ready)
            elif op == "unsubscribe":
                await _handle_unsubscribe(websocket, runtime, consumer_id, raw)
            else:
                await websocket.send_json(
                    {
                        "ok": False,
                        "error_code": "unknown_op",
                        "detail": f"unsupported op: {op!r}",
                    }
                )
    finally:
        pump_task.cancel()
        with suppress(asyncio.CancelledError):
            await pump_task
        await _cleanup_ws_consumer(runtime, consumer_id)


async def _handle_subscribe(
    websocket: WebSocket,
    runtime: Runtime,
    consumer_id: str,
    raw: dict[str, Any],
    queue_ready: asyncio.Event,
) -> None:
    try:
        req = SubscribeRequest.model_validate(raw)
    except ValidationError as exc:
        await websocket.send_json(
            {
                "ok": False,
                "error_code": "invalid_message",
                "detail": exc.json(),
            }
        )
        return

    if not _valid_topic_id(req.topic):
        await websocket.send_json(
            SubscriptionResponse(
                ok=False,
                op="subscribe",
                topic=req.topic,
                error_code="invalid_topic",
                detail="topic must match BASE-QUOTE product id (e.g. BTC-USD)",
            ).model_dump(mode="json")
        )
        return

    before = await runtime.connection_registry.get_status()
    rc_before = _topic_refcount(before, req.topic)

    try:
        await runtime.connection_registry.subscribe(consumer_id, req.topic)
    except ValueError:
        await websocket.send_json(
            SubscriptionResponse(
                ok=False,
                op="subscribe",
                topic=req.topic,
                error_code="unknown_consumer",
                detail="consumer is not registered",
            ).model_dump(mode="json")
        )
        return

    await runtime.broker.subscribe_consumer(req.topic, consumer_id)

    after = await runtime.connection_registry.get_status()
    rc_after = _topic_refcount(after, req.topic)
    if rc_before == 0 and rc_after > 0:
        try:
            await runtime.coinbase_client.subscribe_topic(req.topic)
        except Exception:
            logger.exception("upstream subscribe failed for topic %s", req.topic)
            await runtime.broker.unsubscribe_consumer(req.topic, consumer_id)
            await runtime.connection_registry.unsubscribe(consumer_id, req.topic)
            await websocket.send_json(
                SubscriptionResponse(
                    ok=False,
                    op="subscribe",
                    topic=req.topic,
                    error_code="upstream_subscribe_failed",
                    detail=(
                        "The hub could not complete the upstream subscription for this topic. "
                        "See server logs. The subscribe was not applied."
                    ),
                ).model_dump(mode="json")
            )
            return

    queue_ready.set()

    await websocket.send_json(
        SubscriptionResponse(
            ok=True,
            op="subscribe",
            topic=req.topic,
            error_code=None,
            detail=None,
        ).model_dump(mode="json")
    )


async def _handle_unsubscribe(
    websocket: WebSocket,
    runtime: Runtime,
    consumer_id: str,
    raw: dict[str, Any],
) -> None:
    try:
        req = UnsubscribeRequest.model_validate(raw)
    except ValidationError as exc:
        await websocket.send_json(
            {
                "ok": False,
                "error_code": "invalid_message",
                "detail": exc.json(),
            }
        )
        return

    before = await runtime.connection_registry.get_status()
    rc_before = _topic_refcount(before, req.topic)

    await runtime.connection_registry.unsubscribe(consumer_id, req.topic)
    await runtime.broker.unsubscribe_consumer(req.topic, consumer_id)

    after = await runtime.connection_registry.get_status()
    rc_after = _topic_refcount(after, req.topic)
    if rc_before > 0 and rc_after == 0:
        try:
            await runtime.coinbase_client.unsubscribe_topic(req.topic)
        except Exception:
            logger.exception("upstream unsubscribe failed for topic %s", req.topic)

    await websocket.send_json(
        SubscriptionResponse(
            ok=True,
            op="unsubscribe",
            topic=req.topic,
            error_code=None,
            detail=None,
        ).model_dump(mode="json")
    )


async def _cleanup_ws_consumer(runtime: Runtime, consumer_id: str) -> None:
    status = await runtime.connection_registry.get_status()
    topics: list[str] = []
    for row in status["consumers"]:
        if row["consumer_id"] == consumer_id:
            topics = list(row["topics"])
            break

    await runtime.connection_registry.unregister_consumer(consumer_id)
    await runtime.broker.remove_consumer(consumer_id)

    final = await runtime.connection_registry.get_status()
    for topic in topics:
        if _topic_refcount(final, topic) == 0:
            try:
                await runtime.coinbase_client.unsubscribe_topic(topic)
            except Exception:
                logger.exception("upstream unsubscribe on disconnect for topic %s", topic)

