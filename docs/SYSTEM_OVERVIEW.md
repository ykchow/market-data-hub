# Market Data Hub

## Project Objective

Build a real-time crypto market data hub that ingests market data from the Coinbase WebSocket feed and distributes updates to multiple downstream consumers through a subscribable pub/sub interface.

The system must also expose an MCP (Model Context Protocol) interface so AI agents and LLM-based systems can interact with the same market data through structured tools.

The architecture should support:
- centralized upstream connection ownership
- multiple downstream consumers
- dynamic topic subscriptions
- centralized connection registry
- reference-counted upstream subscriptions
- in-memory snapshot state
- future extensibility for additional exchanges/venues
- AI/LLM usability through clear MCP tool design and documentation

## Authoritative as-built references

This document states **intent and design goals** (often phrased as *should*). **Concrete behavior** in the repository—topic naming validation, JSON shapes, stale rules, WebSocket and MCP contracts, queue overflow policy, and REST response shapes—is defined in [TOPICS.md](./TOPICS.md), [MCP_CONTEXT.md](./MCP_CONTEXT.md), [SYSTEM_ARCHITECTURE.md](./SYSTEM_ARCHITECTURE.md), and the root [README](../README.md). Prefer those sources when wording here is aspirational or generic.

---

# Core Architectural Principles

## Separation of Concerns

The project should be structured into distinct layers:

- Coinbase ingestion layer
- Connection registry layer
- Pub/sub distribution layer
- Snapshot cache layer
- MCP server layer
- REST API layer

Each layer should have isolated responsibilities and minimal coupling.

---

# Technical Stack

## Primary Stack

- Python 3.12+
- FastAPI
- asyncio
- websockets
- pydantic
- pydantic-settings
- uvicorn

## Supporting Components

- Docker
- docker-compose
- pytest
- structured logging

---

# High-Level Architecture

## FastAPI Application Layer

Responsible for:
- application startup/shutdown lifecycle
- dependency initialization
- WebSocket endpoints
- REST endpoints
- runtime orchestration

Primary file:
- app/main.py

---

## Configuration Layer

Responsible for:
- environment variable loading
- runtime configuration
- defaults management

Primary file:
- app/config.py

Example configurations:
- Coinbase WebSocket URL
- default topics
- queue size limits
- stale thresholds
- reconnect intervals
- log levels

---

## Runtime Layer

Responsible for:
- creating shared singleton services
- wiring application dependencies together

Primary file:
- app/runtime.py

Shared services:
- ConnectionRegistry
- PubSubBroker
- SnapshotStore
- CoinbaseClient

---

## Coinbase Ingestion Layer

Responsible for:
- maintaining Coinbase WebSocket connections
- reconnect handling
- upstream subscribe/unsubscribe lifecycle
- parsing inbound messages
- normalizing market data events
- publishing events internally

Primary file:
- app/ingestion/coinbase_client.py

The ingestion layer should remain exchange-specific so additional venues can be added later with minimal impact.

---

## Connection Registry Layer

Responsible for:
- tracking downstream consumers
- tracking active topic subscriptions
- reference-counting subscriptions
- tracking connection uptime
- tracking message metrics
- cleanup on disconnect

Primary file:
- app/registry/connection_registry.py

The registry determines whether upstream subscriptions should exist based on downstream demand.

---

## Pub/Sub Layer

Responsible for:
- fan-out distribution
- topic-based subscriptions
- multi-consumer support
- bounded queues
- backpressure handling

Primary file:
- app/pubsub/broker.py

Each consumer should receive updates independently without blocking other consumers.

---

## Snapshot Store

Responsible for:
- maintaining latest state per topic
- last trade
- best bid
- best ask
- mid price
- timestamp tracking
- stale state detection

Primary file:
- app/cache/snapshot_store.py

The snapshot store is fully in-memory.

---

## MCP Layer

Responsible for exposing AI-friendly tools so LLM agents can interact with the market data hub.

Primary files:
- app/mcp/http_mcp.py
- app/mcp/server.py
- app/mcp/tools.py

**Implemented MCP tools** (agent usage: [MCP_CONTEXT.md](./MCP_CONTEXT.md); handlers: `app/mcp/tools.py`):

- `list_available_topics` — configured defaults, registry refcounts, upstream interest, and per-topic snapshot hints.
- `describe_topic_schema` — JSON Schema for `MarketEvent` and `TopicSnapshot`, plus hub policy fields (stale threshold, queue size).
- `get_topic_snapshot` — latest in-memory snapshot with refcount and upstream-desired context; errors are explicit (`invalid_topic`, `no_snapshot_yet`).
- `subscribe_to_topic_stream` — **does not** deliver a live event stream over MCP; returns **WebSocket `/ws` instructions** so clients attach to the same hub process for normalized `MarketEvent` traffic.

**Spec alignment (explicit):** Some briefs list “subscribe and stream messages” as an MCP tool minimum. This implementation **intentionally does not** multiplex live `MarketEvent` traffic over MCP transports (SSE/stdio). Reasons: volume and latency expectations for market ticks, host and transport limits, and keeping one shared in-process hub for REST, MCP reads, and **`/ws`** fan-out. Agents still get a **single documented path** to streaming (`subscribe_to_topic_stream` → `/ws`).

---

## REST API Layer

Responsible for operational and monitoring endpoints.

Primary file:
- app/api/status_routes.py

Example endpoints:
- /health
- /status
- /topics
- /snapshots/{topic}

---

# Coinbase Constraints

The ingestion layer should consider:
- message throughput constraints
- reconnect handling
- heartbeat handling
- subscription synchronization

The system should minimize unnecessary upstream subscriptions through reference-counted downstream demand.

Only maintain active upstream subscriptions when at least one downstream consumer requires the topic.

---

# Topic Naming Convention

Topics should follow a consistent symbol naming convention.

Examples:
- BTC-USD
- ETH-USD
- SOL-USD

The naming convention should remain consistent across:
- WebSocket subscriptions
- internal pub/sub
- REST APIs
- MCP tools
- documentation

---

# Topic Documentation Requirements

Every supported topic should document:
- topic name
- schema
- example payload
- update cadence
- snapshot semantics
- stale detection rules

Topic documentation should exist in:
- docs/TOPICS.md

---

# Snapshot Semantics

The snapshot layer should maintain the latest known state for each topic.

Snapshots may include:
- last trade price
- best bid
- best ask
- mid price
- event timestamp
- stale indicator

Snapshots are eventually consistent and may temporarily lag during reconnects.

---

# Backpressure Strategy

Each downstream consumer uses a **bounded** `asyncio.Queue` (size from configuration) so memory per client stays capped and slow readers cannot grow buffers without bound.

**As implemented:** when a consumer’s queue is full, the broker **drops the oldest** message for that queue and retains newer events (lossy fan-out per slow consumer; ingestion stays non-blocking). See [TOPICS.md](./TOPICS.md) (WebSocket stream backpressure) and the [README](../README.md) architecture summary.

Other policies (for example disconnecting slow consumers or coalescing per topic) are not the current default; changing them requires updating `app/pubsub/broker.py` and the same documentation in the same change.

---

# Runtime Metrics

The system should expose operational metrics including:
- active downstream consumers
- active upstream subscriptions
- topic subscriber counts
- connection uptime
- inbound message rates
- outbound message rates
- stale topic indicators

Metrics should be available through:
- REST endpoints
- internal runtime state
- optional MCP exposure

**As implemented:** rich aggregates are available from the status REST API (for example `GET /status`, `GET /topics`) implemented in `app/api/status_routes.py`. `GET /status` includes a **`metrics`** object with **cumulative** counters since process start (upstream Coinbase WebSocket messages received, normalized market events successfully published from ingestion, broker queue puts and drop-oldest totals, downstream `/ws` stream frames sent); operators derive rates as Δcount/Δtime. MCP tools return **topic- and snapshot-oriented** fields (for example `registry_refcount`, `upstream_desired`, snapshot freshness in `list_available_topics` / `get_topic_snapshot`) rather than a dedicated metrics tool suite.

---

# Failure Modes

The system and documentation should clearly explain:
- stale topic behavior
- upstream connection drops
- reconnect handling
- unknown symbols/topics
- slow consumer handling
- queue overflow behavior
- snapshot unavailability

AI-facing documentation should explain what errors or responses an agent should expect.

---

# MCP Tool Design Principles

MCP tools must be designed for LLM usability.

Tool definitions should:
- use action-oriented names
- expose strongly typed arguments
- provide concise descriptions
- explain when a tool should be used
- include example inputs and outputs where useful

The MCP layer should allow an LLM to use the system correctly with minimal additional guidance.

---

# AI Context Documentation

The documentation should be written so that an LLM can:
- discover available topics
- understand schemas
- understand expected payloads
- understand failure conditions
- select the correct MCP tools
- query current market state correctly
- subscribe to streams correctly

Live streaming is WebSocket-only; MCP covers discovery, schema, snapshots, and stream setup instructions; see MCP_CONTEXT.md for error codes and limitations.

The goal is for an LLM agent to succeed using only the provided documentation and MCP tool definitions.

---

# Future Extensibility

The architecture should make future exchange integration simple.

Coinbase-specific logic should remain isolated inside the ingestion layer.

Future exchanges should be implemented using separate ingestion modules.

Potential future exchanges:
- Binance
- Kraken
- OKX

The pub/sub, registry, snapshot, and MCP layers should remain exchange-agnostic where possible.

---

# Non-Goals

The following are intentionally out of scope:
- persistent database storage
- authentication/authorization
- distributed deployment
- Kubernetes orchestration
- historical replay systems
- advanced analytics
- order execution
- portfolio management

---

# Restart Behavior

The system uses in-memory state only.

The following are lost on restart:
- cached snapshots
- active subscriptions
- connection metrics
- consumer session state

Consumers must reconnect and resubscribe after restart.

This limitation should be documented clearly.

---

# Deployment Requirements

The project should support:
- local execution
- Docker-based deployment
- single-container deployment
- simple developer setup

The repository should include:
- Dockerfile
- docker-compose.yml
- requirements.txt
- README with setup instructions

---

# Testing Scope

Tests should validate:
- subscription reference counting
- cleanup on disconnect
- snapshot updates
- stale state handling
- WebSocket consumer behavior
- reconnect handling where feasible

Tests should prioritize readability and architecture validation over exhaustive coverage.

---

# Documentation Requirements

The repository should include:
- docs/SYSTEM_ARCHITECTURE.md
- docs/MCP_CONTEXT.md
- docs/TOPICS.md
- docs/AGENT_USAGE.md

The documentation is considered a first-class deliverable.

---

# AI Usage Transparency

Cursor AI and LLM tools are intentionally used for:
- architecture generation
- scaffolding
- documentation drafting
- code generation assistance
- code review assistance

All generated code should be reviewed and validated manually.

AI usage should be documented transparently.

---

# Coding Principles

- Prefer readability over over-engineering
- Use clear comments and docstrings
- Keep files small and focused
- Use explicit async flow
- Avoid hidden magic
- Prefer maintainability over premature optimization
- Keep interfaces simple
- Use structured logging
- Design for explainability during walkthrough/demo

---

# Deliverables

The final project should include:
- source repository
- MCP server
- working local setup
- Docker deployment
- architecture documentation
- AI context documentation
- tests
- README

---

# Important Notes

- Use in-memory state only
- Add structured logs
- Keep the project simple
- Ensure architecture separation is clear
- Ensure the system is understandable by both engineers and LLM-based agents
- Ensure future extensibility remains possible without major redesign