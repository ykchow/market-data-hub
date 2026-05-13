# Topics — Market Data Hub

This document is the **topic contract** for the hub: what a topic is, which ids are expected, how events and snapshots are shaped, how often data moves, and what **stale** / **invalid** mean. It is written for **software engineers** and **LLM agents** using REST, WebSocket, or MCP against the same in-process state (see [ARCHITECTURE.md](./ARCHITECTURE.md) and [PROJECT_SUMMARY.md](./PROJECT_SUMMARY.md)).

---

## 1. What is a topic?

A **topic** is a **product id** string (Coinbase Exchange style) that identifies one traded pair, for example liquid spot USD pairs. The hub uses that string consistently for:

- downstream WebSocket subscribe / unsubscribe control messages;
- internal pub/sub routing and reference counting;
- Coinbase upstream `product_ids` in subscribe frames;
- in-memory snapshot rows;
- REST `/topics`, `/snapshots/{topic}`, and `/status`;
- MCP tools (`list_available_topics`, `describe_topic_schema`, `get_topic_snapshot`, stream instructions).

**Important:** Topics are **not** database keys here; they are in-memory labels. After a process restart, snapshots and subscription state are empty until consumers resubscribe and the feed warms up again.

---

## 2. Supported topics

Two related ideas:

| Concept | Meaning |
|--------|---------|
| **Default catalog** | Configured list `Settings.default_topics` (env `DEFAULT_TOPICS`, JSON array). **Stock default:** `BTC-USD`, `ETH-USD`, `SOL-USD`. Used for discovery and “known good” examples; MCP `list_available_topics` marks topics with `in_default_catalog: true` when they appear in this list. |
| **Subscribable product ids** | Any string that passes the hub’s **topic id pattern** (see §3) may be passed to the WebSocket `subscribe` op. The hub will attempt an upstream Coinbase subscription. Whether Coinbase actually streams data depends on **Coinbase’s** product catalog (invalid or delisted ids may never produce normalized events). |

**For agents:** Prefer ids from `list_available_topics` or the configured defaults. Treat a syntactically valid id with no snapshot and no errors from Coinbase as “possibly unsupported on the wire” until a `MarketEvent` arrives.

---

## 3. Naming convention

The hub validates WebSocket subscribe topics and several MCP arguments with the same rule as `app.main` / `app.mcp.tools`:

- **Pattern (regex):** `^[A-Z0-9]{1,24}-[A-Z0-9]{1,24}$`
- **Shape:** `BASE-QUOTE` — exactly **one** hyphen, **ASCII letters and digits only**, **uppercase** segments of **1–24** characters each.

**Valid:** `BTC-USD`, `ETH-USD`, `SOL-USD`, `ADA-USDT`  
**Invalid:** `btc-usd` (lowercase), `BTC_USD` (wrong separator), `BTC-US` (segment length / shape), `BTC/USD` (slashes), empty or arbitrary free text.

The same string should be used everywhere (WebSocket, REST path segment, MCP `topic` argument).

---

## 4. Schema

Canonical types live in `app/models/market_data.py`. Summaries below match what JSON serializers emit (`MarketEvent` uses string values for `event_type`).

### 4.1 `MarketEvent` (stream / ingestion)

Normalized **incremental** update after Coinbase wire data is parsed. WebSocket stream items use this shape (`model_dump(mode="json")`).

| Field | Type | Meaning |
|-------|------|---------|
| `topic` | string | Product id, e.g. `BTC-USD`. |
| `event_type` | `"trade"` \| `"ticker"` \| `"l2"` | Kind of update. **Current Coinbase mapping uses `ticker` and `trade`** (from `matches` / `match` / `last_match`). `l2` is reserved for future order-book-style normalization. |
| `price` | float \| null | Last trade price when applicable; ticker may also carry a last price. |
| `size` | float \| null | Trade size when applicable (matches). |
| `best_bid` | float \| null | Top-of-book bid when present. |
| `best_ask` | float \| null | Top-of-book ask when present. |
| `exchange_timestamp_ms` | int \| null | Venue time in ms since Unix epoch when parseable from the wire `time` field. |
| `received_at` | string (ISO 8601) | When the hub attached this event (UTC). |

Agents should use `received_at` (and `exchange_timestamp_ms` when present) to reason about **freshness** on the stream. The snapshot store advances its row `updated_at` from `received_at` when merging events.

### 4.2 `TopicSnapshot` (REST / MCP “current view”)

**Materialized** per-topic row for quick reads without replaying the stream.

| Field | Type | Meaning |
|-------|------|---------|
| `topic` | string | Product id. |
| `last_trade_price` | float \| null | Latest merged trade/ticker price field from events. |
| `best_bid` | float \| null | Latest top-of-book bid. |
| `best_ask` | float \| null | Latest top-of-book ask. |
| `mid_price` | float \| null | `(best_bid + best_ask) / 2` **only when both bid and ask are non-null**; otherwise `null`. |
| `updated_at` | string (ISO 8601) \| null | Last time any snapshot field was updated from the stream (hub merge time). |
| `stale` | boolean | Derived at **read** time from `updated_at` and `stale_threshold_seconds` (see §6). |

---

## 5. Update cadence

There is **no fixed millisecond schedule**; cadence is **event-driven** from Coinbase.

- **Upstream channels** (implementation default in `CoinbaseClient`): `ticker` and `matches`.
- **`ticker`:** emits on best bid/ask / last price changes per Coinbase’s rules — typically **high frequency** on active pairs, quieter when the book is static.
- **`matches` / `match` / `last_match`:** emit when **trades** occur — bursty under volume, silent when there are no trades.

Each normalized event **updates the snapshot** (if fields are present) and is **copied to each subscribed consumer queue** for that topic.

**WebSocket stream backpressure:** Each consumer has one **bounded** `asyncio.Queue` (`Settings.queue_size`, default `1000`). On overflow the broker **drops the oldest** item and enqueues the newest (`PubSubBroker` drop-oldest policy). Streams are **lossy** under slow consumers; use snapshots or timestamps if you need “latest known state” rather than every tick.

---

## 6. Snapshot meaning

A **snapshot** is the hub’s **best-effort “now”** view for a topic:

- It aggregates **last trade price**, **best bid**, **best ask**, and derived **mid** from the **most recent** normalized events applied to that topic.
- It is **eventually consistent** with the exchange: reconnects, parse skips, or queue drops can make the stream and snapshot diverge briefly from the venue.
- **`get_snapshot` returns `None`** (REST/MCP surface this as empty or `no_snapshot_yet`) until **at least one** `MarketEvent` has been merged for that topic in this process.

Snapshots **persist in memory** after the last downstream client unsubscribes (rows are not deleted on refcount zero); values may be **old** and marked **`stale`** per §7.

---

## 7. Stale state

**Definition (implementation):** At read time, `SnapshotStore` sets `stale=True` when:

- there is **no** `updated_at`, or  
- **age** = `now_utc - updated_at` **>** `Settings.stale_threshold_seconds` (default **30** seconds, env `STALE_THRESHOLD_SECONDS`).

So **`stale` does not mean “invalid”**; it means **“not confidently live”** — data may still be the last known good values from before a disconnect, quiet market, or subscription gap.

**Agents and dashboards must:**

- Check `snapshot.stale` (and `updated_at`) before quoting a price as live.
- Not treat HTTP 200 or `ok: true` alone as “fresh”; read the typed fields.

**Related operational signals:** `/status` lists snapshot rows flagged stale under `snapshots.stale_topics`. Upstream reconnect behavior is described in [ARCHITECTURE.md](./ARCHITECTURE.md) §6.

---

## 8. Invalid / unknown topic behavior

The hub distinguishes **syntactic** invalid ids from **“no data yet”** valid ids.

### 8.1 Pattern mismatch (`invalid_topic`)

| Surface | Behavior |
|---------|----------|
| **WebSocket** `subscribe` | Responds with `SubscriptionResponse` with `ok: false`, `error_code: "invalid_topic"`, and a short `detail` (pattern must match BASE-QUOTE). **No** registry refcount increment, **no** broker attachment, **no** upstream subscribe for that message. |
| **MCP** `describe_topic_schema`, `get_topic_snapshot`, `subscribe_to_topic_stream` | Returns `ok: false`, `error_code: "invalid_topic"`, with `detail` explaining the pattern. |

So **“unknown” in the sense of random strings or wrong shape** is rejected as **`invalid_topic`**, not silently accepted.

### 8.2 Valid id, no snapshot row yet (`no_snapshot_yet` on MCP)

| Surface | Behavior |
|---------|----------|
| **MCP** `get_topic_snapshot` | If the pattern matches but `SnapshotStore` has no row, returns `ok: false`, `error_code: "no_snapshot_yet"`, plus hints (`registry_refcount`, `active_downstream_demand`, `upstream_desired`). |
| **REST** | `GET /snapshots/{topic}` returns **HTTP 404** with detail `No snapshot data for topic '<id>'` when no row exists yet (no pattern validation on this path; unknown strings simply have no row). |

This covers **valid** product ids that are **new**, **never traded yet**, **rejected upstream** (logged on Coinbase `error` messages), or **subscribed but still warming**.

### 8.3 Coinbase-level errors

Malformed frames are skipped with logs. Coinbase `type: "error"` messages are logged; they may not always propagate as a per-client WebSocket error frame. Engineering assumption: **subscription demand** is tracked by the hub; **data presence** is proven by incoming normalized events and snapshot rows.

---

## 9. Example payloads

### 9.1 `MarketEvent` — ticker-style

```json
{
  "topic": "BTC-USD",
  "event_type": "ticker",
  "price": 98123.45,
  "size": null,
  "best_bid": 98120.0,
  "best_ask": 98125.5,
  "exchange_timestamp_ms": 1715612345678,
  "received_at": "2026-05-13T18:59:05.123456+00:00"
}
```

### 9.2 `MarketEvent` — trade (match)

```json
{
  "topic": "ETH-USD",
  "event_type": "trade",
  "price": 3456.78,
  "size": 0.42,
  "best_bid": null,
  "best_ask": null,
  "exchange_timestamp_ms": 1715612346000,
  "received_at": "2026-05-13T18:59:06.000001+00:00"
}
```

### 9.3 `TopicSnapshot` — live row

```json
{
  "topic": "SOL-USD",
  "last_trade_price": 145.67,
  "best_bid": 145.5,
  "best_ask": 145.8,
  "mid_price": 145.65,
  "updated_at": "2026-05-13T18:59:07.500000+00:00",
  "stale": false
}
```

### 9.4 WebSocket subscribe success

```json
{
  "ok": true,
  "op": "subscribe",
  "topic": "BTC-USD",
  "error_code": null,
  "detail": null
}
```

### 9.5 WebSocket subscribe failure (bad shape)

```json
{
  "ok": false,
  "op": "subscribe",
  "topic": "btc-usd",
  "error_code": "invalid_topic",
  "detail": "topic must match BASE-QUOTE product id (e.g. BTC-USD)"
}
```

---

## 10. Agent checklist (LLM-oriented)

1. Call **`list_available_topics`** (MCP) or **`GET /topics`** (REST) to discover ids and hints (`registry_refcount`, `upstream_desired`, `snapshot.stale`).
2. Call **`describe_topic_schema`** before inferring field types from stream JSON.
3. For “current price” questions, prefer **`get_topic_snapshot`** / **`GET /snapshots/{topic}`** and **read `stale` and `updated_at`**.
4. For live deltas, use **WebSocket `/ws`** with `{"op":"subscribe","topic":"BTC-USD"}`; assume **possible message loss** if the client is slow (bounded queue, drop oldest).
5. If `invalid_topic` → fix the string to match §3. If `no_snapshot_yet` → wait for events, verify Coinbase lists the product, or confirm downstream subscription drove upstream demand.

---

## 11. Document maintenance

When changing `Settings` defaults, Coinbase channels, queue policy, or Pydantic models, update this file in the same change so operators and automated agents stay aligned with code.
