# MCP Context — LLM Agent Guide

This document is written for **LLM agents** (and MCP hosts) that call the Market Data Hub’s Model Context Protocol tools. It mirrors the implementation in `app/mcp/tools.py` and the topic contract in [TOPICS.md](./TOPICS.md). Use it together with tool descriptions from `list_tool_specs()` at runtime.

---

## 1. Purpose of the MCP server

The MCP surface exposes **structured, typed operations** implemented in `app/mcp/tools.py` against a live hub **`Runtime`** (`ConnectionRegistry`, `SnapshotStore`, `CoinbaseClient`, `PubSubBroker`). The FastAPI app uses the **same Python modules and models** for HTTP/WebSocket.

**Process model (read this):** For **one** in-memory hub shared with REST, WebSocket **`/ws`**, and MCP tools, connect your MCP host to the **HTTP+SSE** transport exposed by uvicorn (same process as FastAPI):

- **SSE URL:** `http://127.0.0.1:8000/mcp/sse` when the app listens on port 8000 (see `app/mcp/http_mcp.py`). Hosts that support an MCP “URL” / SSE transport should use this path so tool calls use **`get_runtime()`** from the uvicorn lifespan.

The optional **`python -m app.mcp.server`** entrypoint is a **stdio MCP server in its own OS process** with its **own** `Runtime`. If you run stdio MCP **and** uvicorn together, you still have **two hubs** unless the host uses the SSE URL above. That stdio-only split is noted under [Known limitations](../README.md#known-limitations) and in the [Five-minute smoke test](../README.md#five-minute-smoke-test).

- **Discover** which product ids (topics) the hub knows about and whether there is downstream demand, upstream interest, and snapshot hints.
- **Understand** canonical JSON shapes for live events (`MarketEvent`) and materialized rows (`TopicSnapshot`) without guessing from raw exchange payloads.
- **Read** the latest in-memory **snapshot** per topic (bid, ask, last trade, mid, timestamps, staleness) plus light registry context.

**Non-goals on this MCP surface:** Live multiplexed event streaming is **not** implemented over MCP. The `subscribe_to_topic_stream` tool returns **instructions** to use the hub WebSocket (`/ws`) instead. Process restart clears in-memory snapshots and subscriptions; agents must rediscover and resubscribe.

---

## 2. Response envelope (all tools)

Successful tool results are JSON objects that always include:

| Field | Type | Meaning |
|--------|------|--------|
| `ok` | `true` | Request handled without logical error. |
| `error_code` | `null` | No machine error code. |
| `detail` | `null` | No human-readable error detail. |

Additional fields are merged into the same object (for example `topics`, `snapshot`, `websocket`).

Failures use:

| Field | Type | Meaning |
|--------|------|--------|
| `ok` | `false` | Do not treat numeric fields as authoritative market truth. |
| `error_code` | string | Machine tag (`invalid_topic`, `no_snapshot_yet`, `invalid_arguments`, `unknown_tool`, `internal_error`, …). |
| `detail` | string | Explanation for the model and user. |
| (optional) | … | Extra context, e.g. `topic`, `registry_refcount`, `requested_name`. |

**Agent rule:** Never infer “live price” from `ok: true` alone; always read `snapshot.stale` and `snapshot.updated_at` for snapshot tools.

---

## 3. Available tools

| Tool name | Async | Arguments |
|-----------|--------|-----------|
| `list_available_topics` | Yes | None (empty object `{}`). |
| `describe_topic_schema` | No | `topic` (string, required). |
| `get_topic_snapshot` | Yes | `topic` (string, required). |
| `subscribe_to_topic_stream` | No | `topic` (string, required). |

Valid tool names are exactly this set; any other name yields `unknown_tool`.

---

## 4. When to use each tool

### 4.1 `list_available_topics`

**Use when:** You need topic ids before calling other tools, or you want operational hints (refcount, whether upstream desires the topic, whether a snapshot row exists and if it looks stale).

**Do not use when:** You already have a confirmed topic id and only need prices—in that case `get_topic_snapshot` may suffice, but listing first still reduces typos.

### 4.2 `describe_topic_schema`

**Use when:** You need JSON Schema and field semantics for `MarketEvent` and `TopicSnapshot`, or hub policy numbers (`stale_threshold_seconds`, `queue_size`) to interpret freshness and stream lossiness.

**Do not use when:** You only need the latest numbers—use `get_topic_snapshot` after validating the topic string.

### 4.3 `get_topic_snapshot`

**Use when:** The user asks for **current** bid, ask, last trade, **mid price**, or “what is the market now” for a known product id.

**Do not use when:** You need every tick—use WebSocket per `subscribe_to_topic_stream` instructions.

### 4.4 `subscribe_to_topic_stream`

**Use when:** The user wants **live streaming** or you must explain how to attach to the hub’s real-time feed.

**Expectation:** Response explains that MCP streaming is not wired; it returns WebSocket path, handshake, and subscribe message shape.

### 4.5 Downstream `/ws` lifecycle (reconnect and resubscribe)

Hub topic subscriptions are **per WebSocket connection**, not per user or API key.

- After **`/ws` closes** for any reason (network, timeout, server restart, client navigation), that connection’s topics are **cleared** server-side; opening another WebSocket does **not** restore them.
- The client must **connect again** and send **`subscribe`** once per topic, same as the first session.
- There is **no replay** of missed `MarketEvent`s over `/ws`; expect a gap, then live traffic again.
- For “what is the latest known price after a gap?”, use **`get_topic_snapshot`** or REST **`GET /snapshots/{topic}`** and read **`stale`** / **`updated_at`**.

See [SYSTEM_ARCHITECTURE.md](./SYSTEM_ARCHITECTURE.md) §6.6 and [TOPICS.md](./TOPICS.md) §5.

---

## 5. Argument examples

### 5.1 `list_available_topics`

```json
{}
```

Pass an **empty** object. Any extra properties cause `invalid_arguments`.

### 5.2 `describe_topic_schema`

```json
{ "topic": "BTC-USD" }
```

### 5.3 `get_topic_snapshot`

```json
{ "topic": "ETH-USD" }
```

### 5.4 `subscribe_to_topic_stream`

```json
{ "topic": "SOL-USD" }
```

### 5.5 Topic id rules (all `topic` parameters)

Pattern enforced by the hub: **BASE-QUOTE**, ASCII **uppercase** letters and digits only, **one** hyphen, **1–24** characters per segment:

- Regex: `^[A-Z0-9]{1,24}-[A-Z0-9]{1,24}$`

Valid: `BTC-USD`. Invalid: `btc-usd`, `BTC_USD`, `BTC/USD`.

---

## 6. Expected responses (shapes)

### 6.1 `list_available_topics` — success

Top-level fields include:

- `topics`: array of objects, each with at least:
  - `topic`, `registry_refcount`, `active_downstream_demand`, `upstream_desired`, `in_default_catalog`
  - `snapshot`: either `{ "stale": bool, "updated_at": "<ISO8601>" | null }` or `null` if no row
- `topic_count`: integer
- `default_topics`: array of strings from configuration

### 6.2 `describe_topic_schema` — success

Includes `topic`, `hub_snapshot_policy` (`stale_threshold_seconds`, `queue_size`), `canonical_event` (model name, description, `json_schema`, `event_type_enum`), and `topic_snapshot` (model name, description, `json_schema`).

### 6.3 `get_topic_snapshot` — success

Includes `topic`, `registry_refcount`, `active_downstream_demand`, `upstream_desired`, and `snapshot` as a JSON object matching `TopicSnapshot`:

- `last_trade_price`, `best_bid`, `best_ask`, `mid_price` (float or `null`)
- `updated_at` (ISO string or `null`)
- `stale` (boolean)

### 6.4 `subscribe_to_topic_stream` — success

Includes `topic`, `queue_size_hint`, `streaming_via_mcp: false`, `message`, and `websocket` with `path` (`/ws`), `handshake`, `subscribe_example`, `event_shape`, and **`after_close`** (plain-language rule: new socket + resubscribe each topic; no replay).

### 6.5 Full success examples (top-level objects)

These examples show the **full** tool result shape, including the envelope fields `ok`, `error_code`, and `detail`. Values are illustrative; `queue_size_hint` follows your configured `QUEUE_SIZE`.

**`list_available_topics`** (truncated `topics`; your hub may list more rows):

```json
{
  "ok": true,
  "error_code": null,
  "detail": null,
  "topics": [
    {
      "topic": "BTC-USD",
      "registry_refcount": 1,
      "active_downstream_demand": true,
      "upstream_desired": true,
      "in_default_catalog": true,
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
      "in_default_catalog": true,
      "snapshot": null
    }
  ],
  "topic_count": 2,
  "default_topics": ["BTC-USD", "ETH-USD", "SOL-USD"]
}
```

**`get_topic_snapshot`:**

```json
{
  "ok": true,
  "error_code": null,
  "detail": null,
  "topic": "BTC-USD",
  "registry_refcount": 1,
  "active_downstream_demand": true,
  "upstream_desired": true,
  "snapshot": {
    "topic": "BTC-USD",
    "last_trade_price": 98123.45,
    "best_bid": 98120.0,
    "best_ask": 98125.5,
    "mid_price": 98122.75,
    "updated_at": "2026-05-13T18:59:07.500000+00:00",
    "stale": false
  }
}
```

**`subscribe_to_topic_stream`:**

```json
{
  "ok": true,
  "error_code": null,
  "detail": null,
  "topic": "SOL-USD",
  "queue_size_hint": 1000,
  "streaming_via_mcp": false,
  "message": "Live multiplexed streaming is not implemented on this MCP surface yet. Use the hub WebSocket endpoint with a subscribe control message instead.",
  "websocket": {
    "path": "/ws",
    "handshake": "Standard FastAPI WebSocket upgrade on the same base URL as HTTP.",
    "subscribe_example": { "op": "subscribe", "topic": "SOL-USD" },
    "event_shape": "Same fields as MarketEvent (see describe_topic_schema).",
    "after_close": "Subscriptions are per WebSocket. After any close, open a new /ws connection and send subscribe again for each topic; missed events are not replayed."
  }
}
```

---

## 7. Worked examples

### Example A — Discover then read ETH-USD

1. **Call** `list_available_topics` with `{}`.
2. **Confirm** `ETH-USD` appears (or use default catalog from `default_topics`).
3. **Call** `get_topic_snapshot` with `{ "topic": "ETH-USD" }`.
4. **Report** using `snapshot.best_bid`, `snapshot.best_ask`, `snapshot.last_trade_price`, `snapshot.mid_price`, and explicitly state `snapshot.stale` and `updated_at`.

### Example B — User wants stream of SOL-USD

1. **Call** `subscribe_to_topic_stream` with `{ "topic": "SOL-USD" }`.
2. **Read** `websocket.path`, `websocket.subscribe_example`.
3. **Instruct** the human or client: open WebSocket to `/ws` on the same base URL, send `{"op":"subscribe","topic":"SOL-USD"}` after connect; events match `MarketEvent` (see `describe_topic_schema`). If the socket later drops, they must **reconnect and subscribe again**; the hub does not restore topics on a new socket or replay gaps (see §4.5).

### Example C — Schema before coding

1. **Call** `describe_topic_schema` with `{ "topic": "BTC-USD" }`.
2. **Use** returned `json_schema` blocks to map fields in downstream code or explanations.

---

## 8. Failure modes

| `error_code` | Typical cause | Agent action |
|--------------|---------------|--------------|
| `invalid_topic` | `topic` fails BASE-QUOTE pattern | Fix casing and separator; use ids from `list_available_topics`. |
| `no_snapshot_yet` | Pattern valid but no snapshot row in this process | See §10 (unknown / no-data topic handling). |
| `invalid_arguments` | Missing/non-string `topic`, or arguments passed to `list_available_topics` | Send required string fields; use `{}` for list tool. |
| `unknown_tool` | Host called a name not in the four tools | Use only registered tool names from `list_tool_specs`. |
| `internal_error` | Dispatch bug or unexpected server state (should be rare) | Retry once; if it persists, treat the hub as unhealthy and use logs or `/status`; do not invent prices. |

**`describe_topic_schema`:** A successful response (`ok: true`) only means the `topic` string passed the **BASE-QUOTE** pattern check. It does **not** prove Coinbase lists the product, that upstream has accepted a subscription, or that a snapshot row exists—use **`list_available_topics`**, **`get_topic_snapshot`**, or WebSocket subscribe + events to establish **data presence**.

**Coinbase wire / subscription problems:** Rejected subscriptions, unknown product ids on the exchange, or Coinbase `type: "error"` frames are typically **logged** only. MCP tools do **not** expose a separate `error_code` for Coinbase-specific failures. Infer health from **`registry_refcount`**, **`upstream_desired`**, **`no_snapshot_yet`**, **`snapshot.stale`**, and **`snapshot.updated_at`** (and operator logs or **`GET /status`** when available)—not from a dedicated Coinbase error field in tool results.

**Operational failures (not always separate MCP error codes):** Upstream disconnect, quiet market, or no events yet can yield **stale** snapshots or missing mids (`mid_price` null when bid or ask missing). Until a normalized event merges into the snapshot store, **`get_topic_snapshot`** may remain **`no_snapshot_yet`** even when the id is pattern-valid (see Coinbase note above).

**Stream loss:** WebSocket delivery uses bounded queues; slow consumers may **drop oldest** messages. Prefer snapshot + timestamps for “latest known” state.

---

## 9. How to query current mid price for BTC-USD

1. Ensure the topic string is exactly **`BTC-USD`** (uppercase, hyphen).
2. Call **`get_topic_snapshot`** with arguments:

   ```json
   { "topic": "BTC-USD" }
   ```

3. On **`ok: true`**, read **`snapshot.mid_price`**:
   - If **`mid_price` is a number**, it is \((\text{best\_bid} + \text{best\_ask}) / 2\) from the last merged event that had **both** bid and ask.
   - If **`mid_price` is `null`**, bid or ask was missing in the materialized row; you may report **`best_bid`**, **`best_ask`**, and/or **`last_trade_price`** separately if present, and say mid is unavailable.
4. Always report **`snapshot.stale`** and **`snapshot.updated_at`** when presenting the value as “current” or “live”.

---

## 10. How to handle a stale snapshot

**Definition:** `stale: true` when there is no `updated_at`, or when `now_utc - updated_at` exceeds `stale_threshold_seconds` (default **30**; exposed in `describe_topic_schema` as `hub_snapshot_policy.stale_threshold_seconds`).

**Agent behavior:**

1. **State clearly** that the figure is **not confidently live**; it may be last known good before a disconnect, gap, or quiet period.
2. **Prefer** checking **`list_available_topics`** for the same topic’s `snapshot.stale` / `updated_at` hints, or **`get_topic_snapshot`** again after a short wait if the user needs freshness.
3. **Do not** silently refresh the number without a new tool call; if the user needs streaming freshness, point them to **WebSocket `/ws`** per `subscribe_to_topic_stream`.
4. Optionally mention REST **`/status`** (`snapshots.stale_topics`) for operators; same underlying policy as [TOPICS.md](./TOPICS.md) §7.

---

## 11. How to handle unknown topic

Distinguish three cases:

### 11.1 Syntactically invalid (`invalid_topic`)

Wrong shape (e.g. `btc-usd`, `BTC_USD`). **Fix the string** to match §5.5; do not treat as a supported product id.

### 11.2 Syntactically valid but no snapshot (`no_snapshot_yet`)

The hub has **not** merged any `MarketEvent` for that id in this process. Possible reasons: never subscribed upstream, Coinbase rejected/does not list the product, no trades/tickers yet, or still warming after subscribe.

**Agent steps:**

1. Call **`list_available_topics`** and inspect **`registry_refcount`**, **`upstream_desired`**, and whether a **`snapshot`** hint exists.
2. If there is **no downstream demand** and no upstream interest, explain that something must **subscribe** (WebSocket client) to drive refcount and upstream subscription before data typically flows.
3. If demand exists but still no snapshot, suggest verifying the product exists on Coinbase; wait and retry `get_topic_snapshot`; check logs or `/status` if available.

### 11.3 Valid id, user meant “not in default list”

`list_available_topics` can include topics from refcounts, snapshots, upstream sets, and **defaults**. A topic not in `default_topics` may still be valid if it appears elsewhere in the merged list. Prefer **pattern validation** + **snapshot presence** over guessing from defaults alone.

---

## 12. Quick agent checklist

1. **`list_available_topics`** first when unsure about ids or freshness hints.
2. **`describe_topic_schema`** before interpreting stream or snapshot field types.
3. **`get_topic_snapshot`** for “now” prices; always read **`stale`** and **`updated_at`**.
4. **`subscribe_to_topic_stream`** only for **how to stream** via WebSocket; MCP does not stream events here.
5. On errors, read **`error_code`** and **`detail`**; never invent prices from failed calls.

---

## Related documents

- [README.md](../README.md) — local run, smoke test, and **MCP vs uvicorn** (separate processes).
- [TOPICS.md](./TOPICS.md) — topic naming, `MarketEvent` / `TopicSnapshot` fields, stale rules, WebSocket examples.
- [SYSTEM_ARCHITECTURE.md](./SYSTEM_ARCHITECTURE.md) — data flow, registry, broker, snapshot store, reconnect intent.
- [SYSTEM_OVERVIEW.md](./SYSTEM_OVERVIEW.md) — project scope, MCP design principles, non-goals.
