"""Basic WebSocket client behavior against the FastAPI app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.runtime import get_runtime


def test_ws_client_can_connect() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws"):
            pass


def test_ws_client_can_subscribe() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"op": "subscribe", "topic": "BTC-USD"})
            msg = ws.receive_json()
            assert msg["ok"] is True
            assert msg["op"] == "subscribe"
            assert msg["topic"] == "BTC-USD"


def test_ws_invalid_message_handled_safely() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json([1, 2, 3])
            msg = ws.receive_json()
            assert msg["ok"] is False
            assert msg["error_code"] == "invalid_message"

            ws.send_json({"op": "not_a_real_op"})
            msg2 = ws.receive_json()
            assert msg2["ok"] is False
            assert msg2["error_code"] == "unknown_op"


def test_ws_subscribe_upstream_failure_returns_error_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic = "ZZZZ-USD"

    async def failing_subscribe(_topic: str) -> None:
        raise OSError("simulated upstream subscribe failure")

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            monkeypatch.setattr(
                get_runtime().coinbase_client,
                "subscribe_topic",
                failing_subscribe,
            )
            ws.send_json({"op": "subscribe", "topic": topic})
            msg = ws.receive_json()
    assert msg["ok"] is False
    assert msg["op"] == "subscribe"
    assert msg["topic"] == topic
    assert msg["error_code"] == "upstream_subscribe_failed"
    detail = str(msg.get("detail") or "")
    assert "upstream" in detail.lower()


def test_ws_disconnect_cleanup_does_not_crash() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"op": "subscribe", "topic": "SOL-USD"})
            assert ws.receive_json()["ok"] is True
        # Exiting the WebSocket context closes the connection; cleanup must not raise.
