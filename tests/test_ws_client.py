"""Basic WebSocket client behavior against the FastAPI app."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


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


def test_ws_disconnect_cleanup_does_not_crash() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"op": "subscribe", "topic": "SOL-USD"})
            assert ws.receive_json()["ok"] is True
        # Exiting the WebSocket context closes the connection; cleanup must not raise.
