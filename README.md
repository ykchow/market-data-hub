# Market Data Hub

Real-time crypto market data hub that ingests Coinbase Exchange WebSocket data, normalizes it, keeps in-memory snapshots, and fans updates to multiple downstream consumers via an internal pub/sub layer. The same hub powers operational REST routes and [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) tools for discovery and typed reads.

For deeper detail, see [docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md), [docs/SYSTEM_ARCHITECTURE.md](docs/SYSTEM_ARCHITECTURE.md), [docs/TOPICS.md](docs/TOPICS.md), and [docs/MCP_CONTEXT.md](docs/MCP_CONTEXT.md).

---

## Project overview

- **Single upstream:** One Coinbase WebSocket session per process; downstream clients never talk to Coinbase directly.
- **Demand-driven subscriptions:** `ConnectionRegistry` reference-counts topic interest; upstream subscribe/unsubscribe follows 0→1 and 1→0 transitions.
- **Distribution:** `PubSubBroker` delivers normalized `MarketEvent` objects to each consumer on a dedicated bounded `asyncio.Queue`.
- **Snapshots:** `SnapshotStore` holds the latest bid, ask, last trade, mid, and timestamps per topic for REST and MCP.
- **Interfaces:** FastAPI (`app/main.py`) exposes HTTP status routes, WebSocket `/ws`, and application lifespan; MCP runs as a stdio server (`python -m app.mcp.server`) using the same tool and validation logic as the hub design describes.

**Stack:** Python 3.12+, FastAPI, asyncio, `websockets`, Pydantic / pydantic-settings, uvicorn, pytest, Docker.

---

## Architecture summary

| Layer | Role |
|--------|------|
| **FastAPI** (`app/main.py`) | Routes, WebSocket protocol, lifespan, mounts status API. |
| **Configuration** (`app/config.py`) | Env-driven settings (Coinbase URL, default topics, queue size, stale threshold, reconnect delay, log level). |
| **Runtime** (`app/runtime.py`) | Singletons: `CoinbaseClient`, `ConnectionRegistry`, `PubSubBroker`, `SnapshotStore`. |
| **Ingestion** (`app/ingestion/coinbase_client.py`) | Coinbase I/O, reconnect, subscribe set, parse → canonical events → snapshot + broker. |
| **Registry** (`app/registry/connection_registry.py`) | Consumers, per-topic refcounts, metrics, cleanup on disconnect. |
| **Pub/sub** (`app/pubsub/broker.py`) | Topic fan-out; bounded queues; **drop oldest** on overflow for slow consumers. |
| **Snapshots** (`app/cache/snapshot_store.py`) | Per-topic materialized view; **stale** derived at read time from `updated_at` vs `stale_threshold_seconds`. |
| **MCP** (`app/mcp/server.py`, `app/mcp/tools.py`) | stdio MCP server; tools call shared hub types and policies. |
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

---

## REST endpoint examples

Base URL: `http://127.0.0.1:8000` (adjust for host/port).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Minimal index: links to health, status, WebSocket path. |
| GET | `/health` | Liveness; includes `runtime` readiness (`ready` vs `not_initialized`). |
| GET | `/status` | Registry summary, snapshot counts, stale topic list, upstream desired topics. |
| GET | `/topics` | Merged topic list with refcount, upstream flag, optional snapshot freshness hints. |
| GET | `/snapshots/{topic}` | Full `TopicSnapshot` plus refcount metadata; **404** if no snapshot row exists yet. |

**Examples (curl)**

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/status
curl -s http://127.0.0.1:8000/topics
curl -s http://127.0.0.1:8000/snapshots/BTC-USD
```

Status routes **do not** mutate subscriptions; driving upstream demand is done via WebSocket subscribe (or another consumer that uses the registry).

---

## MCP usage summary

MCP exposes **four** tools (see [docs/MCP_CONTEXT.md](docs/MCP_CONTEXT.md)):

| Tool | Purpose |
|------|---------|
| `list_available_topics` | Discovery; pass `{}` only. Returns topics with refcount, upstream hints, snapshot hints, `default_topics`. |
| `describe_topic_schema` | JSON Schema and hub policy (`stale_threshold_seconds`, `queue_size`) for `MarketEvent` and `TopicSnapshot`. |
| `get_topic_snapshot` | Current materialized row; always interpret **`snapshot.stale`** and **`snapshot.updated_at`**. |
| `subscribe_to_topic_stream` | **Does not stream over MCP.** Returns instructions to use WebSocket **`/ws`** on the HTTP app (path, subscribe message shape, event shape). |

**Run the MCP server (stdio):**

```bash
python -m app.mcp.server
```

Configure your MCP host (e.g. Cursor, Claude Desktop) to spawn that command with `cwd` set to this repository and the same Python environment that has `mcp` and project dependencies installed. See comments in `app/mcp/server.py` for a JSON snippet.

**Response envelope:** Successful tool results include `ok: true`, `error_code: null`, `detail: null`, plus tool-specific fields. Failures set `ok: false` and a machine `error_code` (`invalid_topic`, `no_snapshot_yet`, `invalid_arguments`, `unknown_tool`, …).

**Important:** The stdio MCP process starts its **own** `Runtime` in-process. If you also run `uvicorn` for HTTP/WebSocket, you have **two separate processes** and **two separate in-memory hubs** unless you add a remote bridge. For demos, either use REST/WebSocket only, or MCP only, or accept that tool results reflect the MCP process’s hub, not the uvicorn process.

---

## Known limitations

- **In-memory only:** No database; no historical replay; no crash durability for snapshots or metrics.
- **Single process:** Not a distributed or multi-region HA design; vertical scale limits apply.
- **No auth:** No authentication or authorization on HTTP/WebSocket (out of scope).
- **Coinbase-only ingestion:** Other venues would need new ingestion modules; topic naming follows Coinbase-style product ids.
- **MCP streaming:** Live tick streams are **not** multiplexed through MCP; agents use **`subscribe_to_topic_stream`** for WebSocket instructions only.
- **Valid id ≠ data:** Syntactically valid topics may never receive data (delisted product, upstream error, no activity); `get_topic_snapshot` may return `no_snapshot_yet` until at least one normalized event is merged.
- **Separate MCP vs HTTP processes:** Two runtimes if both are started (see above).

Non-goals from the product brief include order execution, portfolio management, Kubernetes-specific docs, and advanced analytics ([docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md)).

---

## Restart behavior

All hub state is **volatile**:

- Cached **snapshots**, **active WebSocket subscriptions**, **registry refcounts**, **metrics**, and **consumer session** state are **lost** on process restart.
- **Consumers must reconnect** to `/ws` and **send subscribe messages again**.
- Snapshot rows **reappear only after** new `MarketEvent` data is merged for each topic.

This is expected behavior for an in-memory hub, not a defect.

---

## Walkthrough notes

- **Story arc:** Start at `/health` and `/status`, open `/ws` and subscribe to `BTC-USD`, watch `MarketEvent` frames, then compare **`GET /snapshots/BTC-USD`** to the stream (one truth from `SnapshotStore`).
- **Refcount demo:** Two browser tabs or two WebSocket clients subscribing to the same topic show refcount > 1; unsubscribe one tab and show upstream remains until the last consumer drops the topic.
- **Stale semantics:** After pausing the upstream or waiting past `STALE_THRESHOLD_SECONDS`, show `stale: true` while values may still be present ([docs/TOPICS.md](docs/TOPICS.md) §7).
- **Code map:** Ingestion and Coinbase protocol details stay in `app/ingestion/`; registry and broker explain “who wants what” and “how delivery is isolated”; `app/models/market_data.py` defines the canonical JSON shapes.
- **AI transparency:** AI-assisted scaffolding and docs are acknowledged in [docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md) and [docs/AI_USAGE.md](docs/AI_USAGE.md); validate behavior against running services and tests.

---

## Documentation index

| Document | Content |
|----------|---------|
| [docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md) | Objectives, principles, non-goals, deliverables. |
| [docs/SYSTEM_ARCHITECTURE.md](docs/SYSTEM_ARCHITECTURE.md) | Components, lifecycle, reconnect intent, backpressure, failure matrix. |
| [docs/TOPICS.md](docs/TOPICS.md) | Topic contract, schemas, stale rules, WebSocket examples. |
| [docs/MCP_CONTEXT.md](docs/MCP_CONTEXT.md) | MCP tool usage, envelopes, agent checklist. |
