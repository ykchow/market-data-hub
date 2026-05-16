# Market Data Hub

Real-time crypto market data hub that ingests Coinbase Exchange WebSocket data, normalizes it, keeps in-memory snapshots, and fans updates to multiple downstream consumers via an internal pub/sub layer. The same hub powers operational REST routes and [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) tools for discovery and typed reads.

For deeper detail, see [docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md), [docs/SYSTEM_ARCHITECTURE.md](docs/SYSTEM_ARCHITECTURE.md), [docs/TOPICS.md](docs/TOPICS.md), and [docs/MCP_CONTEXT.md](docs/MCP_CONTEXT.md).

---

## Getting started

### Suggested reading order

1. **This README** — install, run, REST and WebSocket examples, MCP over **SSE on uvicorn** (same `Runtime` as HTTP) vs optional stdio MCP.
2. **[docs/TOPICS.md](docs/TOPICS.md)** — topic id rules, `MarketEvent` / `TopicSnapshot` shapes, stale semantics, and WebSocket payload examples (the contract for every surface).
3. **[docs/MCP_CONTEXT.md](docs/MCP_CONTEXT.md)** — if you call hub tools from an MCP host or an LLM agent: tool arguments, response envelope, failure codes, and checklists.
4. **[docs/SYSTEM_ARCHITECTURE.md](docs/SYSTEM_ARCHITECTURE.md)** — refcount lifecycle, pub/sub and snapshot roles, reconnect intent, and backpressure.
5. **[docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md)** — product goals, non-goals, and how this document relates to as-built docs.

### Five-minute smoke test

Prerequisites: Python **3.12+**, dependencies installed ([Setup instructions](#setup-instructions)), optional `.env` with defaults.

1. **Start the HTTP app** (repository root, virtual environment activated):

   ```bash
   uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

2. **Probe REST** (second shell):

   ```bash
   curl -s http://127.0.0.1:8000/health
   curl -s http://127.0.0.1:8000/topics
   ```

3. **Subscribe over WebSocket** so downstream demand and upstream interest can register: connect to **`ws://127.0.0.1:8000/ws`**, send `{"op":"subscribe","topic":"BTC-USD"}`, and confirm you receive a JSON acknowledgement with `"ok": true` (full shapes in [WebSocket usage example](#websocket-usage-example)). Any WebSocket client works; with the project’s `websockets` package you can run:

   ```python
   import asyncio, json, websockets

   async def main() -> None:
       async with websockets.connect("ws://127.0.0.1:8000/ws") as ws:
           await ws.send(json.dumps({"op": "subscribe", "topic": "BTC-USD"}))
           print(await ws.recv())

   asyncio.run(main())
   ```

   Save as a file (for example `smoke_ws.py`) in the repo root and run `python smoke_ws.py`, or paste into a REPL.

4. **Read a snapshot** once the feed has merged at least one event (retry if you still see **404**):

   ```bash
   curl -s http://127.0.0.1:8000/snapshots/BTC-USD
   ```

5. **Optional — MCP tool (same hub as steps 2–4)** — With uvicorn still running, configure your MCP host to use the **SSE URL** **`http://127.0.0.1:8000/mcp/sse`** (transport type “SSE” / URL in Cursor; see [MCP usage summary](#mcp-usage-summary)), then invoke **`list_available_topics`** with **`{}`**. That uses the **same in-memory hub** as REST and `/ws`. Alternatively, **`python -m app.mcp.server`** is a **separate process** with its **own** `Runtime` ([Known limitations](#known-limitations)); use it only when you intentionally want a standalone stdio server.

---

## Project overview

- **Single upstream:** One Coinbase WebSocket session per process; downstream clients never talk to Coinbase directly.
- **Demand-driven subscriptions:** `ConnectionRegistry` reference-counts topic interest; upstream subscribe/unsubscribe follows 0→1 and 1→0 transitions.
- **Distribution:** `PubSubBroker` delivers normalized `MarketEvent` objects to each consumer on a dedicated bounded `asyncio.Queue`.
- **Snapshots:** `SnapshotStore` holds the latest bid, ask, last trade, mid, and timestamps per topic for REST and MCP.
- **Interfaces:** FastAPI (`app/main.py`) exposes HTTP status routes, WebSocket `/ws`, MCP over **SSE** at **`/mcp/sse`** (shared `Runtime`), application lifespan; optional stdio MCP (`python -m app.mcp.server`) reuses the same tool handlers in a second process if needed.

**Stack:** Python 3.12+, FastAPI, asyncio, `websockets`, Pydantic / pydantic-settings, uvicorn, pytest, Docker.

---

## Architecture summary

| Layer | Role |
|--------|------|
| **FastAPI** (`app/main.py`) | Routes, WebSocket protocol, lifespan, mounts status API. |
| **Configuration** (`app/config.py`) | Env-driven settings (Coinbase URL, default topics, queue size, stale threshold, reconnect delay, log level). |
| **Runtime** (`app/runtime.py`) | Singletons: `HubMetrics`, `CoinbaseClient`, `ConnectionRegistry`, `PubSubBroker`, `SnapshotStore`. |
| **Ingestion** (`app/ingestion/coinbase_client.py`) | Coinbase I/O, reconnect, subscribe set, parse → canonical events → snapshot + broker. |
| **Registry** (`app/registry/connection_registry.py`) | Consumers, per-topic refcounts, metrics, cleanup on disconnect. |
| **Pub/sub** (`app/pubsub/broker.py`) | Topic fan-out; bounded queues; **drop oldest** on overflow for slow consumers. |
| **Snapshots** (`app/cache/snapshot_store.py`) | Per-topic materialized view; **stale** derived at read time from `updated_at` vs `stale_threshold_seconds`. |
| **MCP** (`app/mcp/http_mcp.py`, `app/mcp/server.py`, `app/mcp/tools.py`) | SSE on uvicorn (`/mcp/sse` + `/mcp/messages/`) shares `Runtime` with HTTP/WebSocket; optional stdio (`python -m app.mcp.server`) for hosts without URL transport. |
| **Status API** (`app/api/status_routes.py`) | `/health`, `/status`, `/topics`, `/snapshots/{topic}`. |

Data flow: Coinbase → normalize → update snapshot → `publish` to subscriber queues; control plane (subscribe/unsubscribe/disconnect) updates registry and upstream desired set.

---

## Setup instructions

1. **Python:** Use Python **3.12+**.
2. **Clone** the repository and create a virtual environment (recommended).
3. **Install dependencies** from the repo root:

   ```bash
   pip install -r requirements.txt
   ```

4. **Environment:** Copy or edit `.env` in the project root (see [Configuration](#configuration)). `docker-compose` expects this file to exist (it can be minimal).

---

## Configuration

Settings are loaded from environment variables and optional `.env` (see `app/config.py`).

| Variable | Default | Purpose |
|----------|---------|---------|
| `COINBASE_WS_URL` | `wss://ws-feed.exchange.coinbase.com` | Coinbase Exchange WebSocket URL. |
| `DEFAULT_TOPICS` | `["BTC-USD","ETH-USD","SOL-USD"]` | JSON array; discovery defaults. |
| `QUEUE_SIZE` | `1000` | Max items per consumer queue. |
| `STALE_THRESHOLD_SECONDS` | `30` | Snapshot marked stale when older than this (seconds). |
| `RECONNECT_DELAY_SECONDS` | `1` | Base delay before upstream reconnect retry. |
| `LOG_LEVEL` | `INFO` | Logging level. |

---

## Run locally

From the repository root (with your venv activated):

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For development reload:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

The service listens on **port 8000** by default (same as Docker).

---

## Run with Docker

Build and run with Compose (maps host `8000` → container `8000`, loads `.env`):

```bash
docker compose up --build
```

Or build and run the image directly:

```bash
docker build -t market-data-hub .
docker run --rm -p 8000:8000 --env-file .env market-data-hub
```

The container runs: `uvicorn app.main:app --host 0.0.0.0 --port 8000`.

---

## Test instructions

From the repo root:

```bash
pytest
```

Tests under `tests/` cover registry refcounts, snapshot behavior, WebSocket client flows, and related invariants (see [docs/SYSTEM_ARCHITECTURE.md](docs/SYSTEM_ARCHITECTURE.md) §13).

---

## WebSocket usage example

Connect to **`ws://<host>:<port>/ws`** (or `wss://` behind TLS).

**Client → server (JSON text frames)**

Subscribe to a product id (topic):

```json
{"op": "subscribe", "topic": "BTC-USD"}
```

Unsubscribe:

```json
{"op": "unsubscribe", "topic": "BTC-USD"}
```

**Server → client**

- After each control message, an acknowledgement:

  ```json
  {"ok": true, "op": "subscribe", "topic": "BTC-USD", "error_code": null, "detail": null}
  ```

- Stream payloads are normalized **`MarketEvent`** objects (same field names as in [docs/TOPICS.md](docs/TOPICS.md)): `topic`, `event_type`, `price`, `size`, `best_bid`, `best_ask`, `exchange_timestamp_ms`, `received_at`.

Topic ids must match **`BASE-QUOTE`**: uppercase ASCII letters/digits, one hyphen, segments 1–24 chars (regex `^[A-Z0-9]{1,24}-[A-Z0-9]{1,24}$`). Invalid topics receive `ok: false`, `error_code: "invalid_topic"`, without changing upstream state.

**Backpressure:** Each connection has a bounded queue; on overflow the broker **drops the oldest** message. Treat the stream as **lossy** under load; use snapshots or timestamps for “latest known” state.

**Reconnect:** Subscriptions are **per WebSocket**. If `/ws` closes for any reason, open a new connection and send `{"op":"subscribe","topic":"…"}` again for every topic you need. The hub does **not** auto-restore topics on a new socket and does **not** replay missed events over `/ws` (details in [docs/TOPICS.md](docs/TOPICS.md) §5 and [docs/SYSTEM_ARCHITECTURE.md](docs/SYSTEM_ARCHITECTURE.md) §6.6).

---

## REST endpoint examples

Base URL: `http://127.0.0.1:8000` (adjust for host/port).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Minimal index: links to health, status, WebSocket path. |
| GET | `/health` | Liveness; includes `runtime` readiness (`ready` vs `not_initialized`). |
| GET | `/status` | Registry summary, snapshot counts, stale topic list, upstream desired topics, **cumulative `metrics`** (derive rates as Δ/Δt). |
| GET | `/topics` | Merged topic list with refcount, upstream flag, optional snapshot freshness hints. |
| GET | `/snapshots/{topic}` | Full `TopicSnapshot` plus refcount metadata; **404** if no snapshot row exists yet. |

**Examples (curl)**

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/status
curl -s http://127.0.0.1:8000/topics
curl -s http://127.0.0.1:8000/snapshots/BTC-USD
```

**Truncated response examples (illustrative)** — shapes match `app/api/status_routes.py`; values depend on live subscriptions and data.

`GET /status`:

```json
{
  "registry": {
    "active_downstream_consumers": 1,
    "topic_reference_counts": { "BTC-USD": 1 },
    "topics_with_active_demand": ["BTC-USD"],
    "total_consumer_topic_pairs": 1,
    "consumers": [
      {
        "consumer_id": "a1b2c3d4e5f647891234567890abcdef",
        "topics": ["BTC-USD"],
        "connected_for_seconds": 12.345
      }
    ]
  },
  "snapshots": {
    "topic_count": 1,
    "stale_topic_count": 0,
    "stale_topics": []
  },
  "upstream": {
    "coinbase_desired_topics": ["BTC-USD"],
    "coinbase_desired_topic_count": 1
  },
  "metrics": {
    "cumulative_since_process_start": true,
    "process_uptime_seconds": 120.5,
    "upstream_websocket_messages_received": 4500,
    "upstream_normalized_market_events": 2100,
    "broker_queue_puts": 2100,
    "broker_drop_oldest_total": 0,
    "downstream_ws_stream_messages_sent": 2100
  }
}
```

`GET /topics` (merged names from registry, snapshot store, and upstream desired set; **no** `default_topics` field here—unlike MCP `list_available_topics`):

```json
{
  "topics": [
    {
      "topic": "BTC-USD",
      "registry_refcount": 1,
      "active_downstream_demand": true,
      "upstream_desired": true,
      "snapshot": {
        "stale": false,
        "updated_at": "2026-05-13T18:59:07.500000+00:00"
      }
    },
    {
      "topic": "ETH-USD",
      "registry_refcount": 0,
      "active_downstream_demand": false,
      "upstream_desired": false,
      "snapshot": null
    }
  ],
  "topic_count": 2
}
```

Status routes **do not** mutate subscriptions; driving upstream demand is done via WebSocket subscribe (or another consumer that uses the registry).

---

## MCP usage summary

**Recommended:** With **`uvicorn app.main:app`** running, add an MCP server whose URL is **`http://127.0.0.1:8000/mcp/sse`** (SSE transport). Tool calls then use the **same** `Runtime` as `/health`, `/ws`, and `/topics`. The service index **`GET /`** includes **`mcp_sse`** with this path.

MCP exposes **four** tools (see [docs/MCP_CONTEXT.md](docs/MCP_CONTEXT.md)):

| Tool | Purpose |
|------|---------|
| `list_available_topics` | Discovery; pass `{}` only. Returns topics with refcount, upstream hints, snapshot hints, `default_topics`. |
| `describe_topic_schema` | JSON Schema and hub policy (`stale_threshold_seconds`, `queue_size`) for `MarketEvent` and `TopicSnapshot`. |
| `get_topic_snapshot` | Current materialized row; always interpret **`snapshot.stale`** and **`snapshot.updated_at`**. |
| `subscribe_to_topic_stream` | **Does not stream over MCP.** Returns instructions to use WebSocket **`/ws`** (path, subscribe shape, event shape, **`after_close`** hint: new socket + resubscribe each topic; no replay). |

**Optional — stdio MCP (separate process):**

```bash
python -m app.mcp.server
```

Use this only when your host cannot attach via URL. It starts its **own** `Runtime` (a second hub if uvicorn is also running). Configure the host to spawn that command with `cwd` set to this repository and the same Python environment that has `mcp` and project dependencies installed. See comments in `app/mcp/server.py` for a JSON snippet.

**SSE on uvicorn:** No extra command beyond uvicorn. The MCP host uses transport **SSE** and URL **`http://<host>:<port>/mcp/sse`**; the stream tells the client to **`POST`** JSON-RPC to **`/mcp/messages/?session_id=…`** (handled by `mcp.server.sse.SseServerTransport`).

**Response envelope:** Successful tool results include `ok: true`, `error_code: null`, `detail: null`, plus tool-specific fields. Failures set `ok: false` and a machine `error_code` (`invalid_topic`, `no_snapshot_yet`, `invalid_arguments`, `unknown_tool`, …).

---

## Known limitations

- **In-memory only:** No database; no historical replay; no crash durability for snapshots or metrics.
- **Single process:** Not a distributed or multi-region HA design; vertical scale limits apply.
- **No auth:** No authentication or authorization on HTTP/WebSocket (out of scope).
- **Coinbase-only ingestion:** Other venues would need new ingestion modules; topic naming follows Coinbase-style product ids.
- **MCP streaming (intentional trade-off):** Live tick streams are **not** multiplexed through MCP; agents use **`subscribe_to_topic_stream`** for WebSocket **`/ws`** instructions only. This differs from a strict reading of briefs that require streaming *on* MCP; rationale and spec-alignment note are in [docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md) (MCP Layer).
- **Valid id ≠ data:** Syntactically valid topics may never receive data (delisted product, upstream error, no activity); `get_topic_snapshot` may return `no_snapshot_yet` until at least one normalized event is merged.
- **stdio MCP vs uvicorn:** Running **`python -m app.mcp.server`** alongside uvicorn still yields **two** in-memory hubs; prefer the **SSE URL** on the HTTP app for one hub.

Non-goals from the product brief include order execution, portfolio management, Kubernetes-specific docs, and advanced analytics ([docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md)).

---

## Restart behavior

All hub state is **volatile**:

- Cached **snapshots**, **active WebSocket subscriptions**, **registry refcounts**, **metrics**, and **consumer session** state are **lost** on process restart.
- **Consumers must reconnect** to `/ws` and **send subscribe messages again**.
- Snapshot rows **reappear only after** new `MarketEvent` data is merged for each topic.

This is expected behavior for an in-memory hub, not a defect.

---

## Documentation index

| Document | Content |
|----------|---------|
| [docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md) | Objectives, principles, non-goals, deliverables. |
| [docs/SYSTEM_ARCHITECTURE.md](docs/SYSTEM_ARCHITECTURE.md) | Components, lifecycle, reconnect intent, backpressure, failure matrix. |
| [docs/TOPICS.md](docs/TOPICS.md) | Topic contract, schemas, stale rules, WebSocket examples. |
| [docs/MCP_CONTEXT.md](docs/MCP_CONTEXT.md) | MCP tool usage, envelopes, agent checklist. |
