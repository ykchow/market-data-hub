# Cursor AI Prompt Guide — Market Data Hub

This document contains the recommended Cursor AI prompts for building the `market-data-hub` assignment incrementally and safely.

The workflow is intentionally architecture-first and implementation-second to:
- reduce debugging complexity
- avoid overengineering
- maintain architectural consistency
- improve explainability during walkthroughs
- align with assignment evaluation criteria

---

# Recommended Workflow

1. Create `SYSTEM_OVERVIEW.md`
2. Generate `SYSTEM_ARCHITECTURE.md`
3. Generate architecture diagrams
4. Generate folder structure only
5. Implement files incrementally in dependency order
6. Generate documentation
7. Generate deployment files
8. Run tests and refine
9. Reviews

---

# Prompts for File Generation In Order

## 1. app/config.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Implement app/config.py only.

Requirements:
- use pydantic-settings
- define a Settings class
- include Coinbase WebSocket URL
- include default topics such as BTC-USD, ETH-USD, SOL-USD
- include queue size
- include stale threshold seconds
- include reconnect delay
- include log level
- load from .env
- expose get_settings()
- keep it simple and readable
- add comments/docstrings
```

---

## 2. app/models/market_data.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Implement app/models/market_data.py only.

Requirements:
- define Pydantic models used across the system
- include market data event model
- include snapshot model
- include subscription request/response models
- include connection status model
- include topic status model
- use clear type hints
- keep models simple
- add comments explaining each model
```

---

## 3. app/registry/connection_registry.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Implement app/registry/connection_registry.py only.

Requirements:
- track downstream consumer connections
- track topic subscriptions per consumer
- maintain reference counts per topic
- support register_consumer()
- support unregister_consumer()
- support subscribe()
- support unsubscribe()
- cleanup all subscriptions on disconnect
- expose status/metrics
- use asyncio.Lock for safe updates
- keep logic simple and easy to explain
- add comments/docstrings
```

---

## 4. app/cache/snapshot_store.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Implement app/cache/snapshot_store.py only.

Requirements:
- maintain latest in-memory snapshot per topic
- update snapshot from normalized market data events
- support get_snapshot(topic)
- support list_snapshots()
- support stale detection based on configured threshold
- no database
- use simple in-memory dictionary
- add comments/docstrings
```

---

## 5. app/pubsub/broker.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Implement app/pubsub/broker.py only.

Requirements:
- asyncio-based pub/sub broker
- support multiple consumers per topic
- each consumer has a bounded asyncio.Queue
- support subscribe_consumer(topic, consumer_id)
- support unsubscribe_consumer(topic, consumer_id)
- support publish(topic, message)
- support get_consumer_queue(consumer_id)
- implement simple backpressure strategy: if queue is full, drop the oldest message and enqueue the latest
- add comments explaining this strategy
- keep implementation simple
```

---

## 6. app/ingestion/coinbase_client.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Implement app/ingestion/coinbase_client.py only.

Requirements:
- create CoinbaseClient class
- connect to Coinbase WebSocket feed
- support start() and stop()
- support subscribe_topic(topic)
- support unsubscribe_topic(topic)
- maintain active upstream subscriptions
- parse Coinbase messages into normalized market data events
- publish normalized events to PubSubBroker
- update SnapshotStore
- include reconnect loop
- handle malformed messages safely
- keep implementation understandable, not overengineered
- add comments/docstrings
```

---

## 7. app/runtime.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Implement app/runtime.py only.

Requirements:
- create a Runtime class that owns shared services
- initialize Settings
- initialize ConnectionRegistry
- initialize SnapshotStore
- initialize PubSubBroker
- initialize CoinbaseClient
- provide startup() and shutdown() methods
- expose a get_runtime() dependency/helper
- avoid circular imports
- keep dependency wiring simple
```

---

## 8. app/api/status_routes.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Implement app/api/status_routes.py only.

Requirements:
- create FastAPI APIRouter
- expose GET /health
- expose GET /status
- expose GET /topics
- expose GET /snapshots/{topic}
- use runtime services
- return simple JSON responses
- include connection/subscription/snapshot status where available
- add comments/docstrings
```

---

## 9. app/main.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Implement app/main.py only.

Requirements:
- create FastAPI app
- use lifespan startup/shutdown to start and stop runtime
- include status_routes router
- expose WebSocket endpoint for downstream consumers
- WebSocket should allow subscribe/unsubscribe messages
- WebSocket should stream topic updates from PubSubBroker queues
- cleanup registry and broker subscriptions on disconnect
- keep WebSocket protocol simple and documented in comments
- avoid overengineering
```

---

## 10. app/mcp/tools.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Implement app/mcp/tools.py only.

Requirements:
- define MCP tool functions for AI agents
- tools should include:
  - list_available_topics
  - describe_topic_schema
  - get_topic_snapshot
  - subscribe_to_topic_stream placeholder if full streaming is difficult
- tool names and descriptions should be LLM-friendly
- use action-oriented naming
- return clear structured data
- handle unknown topic errors clearly
- add comments explaining intended MCP usage
```

---

## 11. app/mcp/server.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md
- app/mcp/tools.py

Implement app/mcp/server.py only.

Requirements:
- create MCP server entrypoint
- register tools from app/mcp/tools.py
- keep setup simple
- include comments explaining how to run/use the MCP server
- do not duplicate business logic here
```

---

## 12. tests/test_registry.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md
- app/registry/connection_registry.py

Implement tests/test_registry.py only.

Test:
- registering consumer
- subscribing consumer to topic
- reference count increments
- multiple consumers on same topic
- unsubscribe decrements count
- disconnect cleanup removes subscriptions
- no leaked subscriptions after unregister

Keep tests simple and readable.
```

---

## 13. tests/test_snapshot_store.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md
- app/cache/snapshot_store.py
- app/models/market_data.py

Implement tests/test_snapshot_store.py only.

Test:
- snapshot is created from market data event
- get_snapshot returns latest state
- list_snapshots returns all snapshots
- stale detection works
- unknown topic returns expected empty/not-found response

Keep tests simple and readable.
```

---

## 14. tests/test_ws_client.py

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md
- app/main.py

Implement tests/test_ws_client.py only.

Test basic WebSocket behavior:
- client can connect
- client can send subscribe message
- invalid message is handled safely
- client disconnect cleanup does not crash

Use FastAPI TestClient if appropriate.
Keep test simple.
```

---

# Documentation Generation

## docs/TOPICS.md

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md
- app/models/market_data.py

Generate docs/TOPICS.md.

Include:
- supported topics
- naming convention
- schema
- update cadence
- example payloads
- snapshot meaning
- stale state explanation
- unknown topic behavior

Write it so both engineers and LLM agents can understand it.
```

---

## docs/MCP_CONTEXT.md

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md
- app/mcp/tools.py
- docs/TOPICS.md

Generate docs/MCP_CONTEXT.md.

Write this document for an LLM agent.

Include:
- purpose of the MCP server
- available tools
- when to use each tool
- argument examples
- expected responses
- worked examples
- failure modes
- how to query current mid price for BTC-USD
- how to handle stale snapshot
- how to handle unknown topic
```

---

## docs/AI_USAGE.md

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Generate docs/AI_USAGE.md.

Explain transparently how Cursor AI / LLM assistance was used:
- requirements summarization
- architecture drafting
- code scaffolding
- implementation assistance
- documentation drafting
- review and refinement

Also state that generated code was reviewed and validated manually.
Keep it professional and concise.
```

---

# Deployment Files

## requirements.txt

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md
- all implemented Python files

Now generate requirements.txt.

Requirements:
- include only libraries actually used in the project
- avoid unnecessary dependencies
- include testing dependencies
- use stable package versions
- keep the dependency list minimal and production-appropriate

Also briefly explain why each dependency is required.
```

---

## Dockerfile

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md
- requirements.txt

Generate Dockerfile only.

Requirements:
- Python slim image
- install requirements
- copy project files
- expose app port
- run uvicorn app.main:app
- keep it simple
- suitable for single-container deployment
```

---

## docker-compose.yml

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md
- Dockerfile

Generate docker-compose.yml only.

Requirements:
- one service for market-data-hub
- build from current directory
- map port 8000 or 9000 clearly
- load .env if available
- restart unless-stopped
- keep it simple
```

---

## .env.example

```text
Read:
- app/config.py

Generate .env.example only.

Include all configurable environment variables used by app/config.py.
Use safe example values.
Do not include secrets.
```

---

# README Generation

## README.md

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md
- docs/TOPICS.md
- docs/MCP_CONTEXT.md

Generate README.md.

Include:
- project overview
- architecture summary
- setup instructions
- run locally
- run with Docker
- test instructions
- WebSocket usage example
- REST endpoint examples
- MCP usage summary
- known limitations
- restart behavior
- walkthrough notes
```

---

# REVIEWS

## Architecture Integrity Review

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Review the entire project for architectural integrity.

Check for:
- architecture inconsistencies
- separation-of-concerns violations
- tight coupling between layers
- duplicated responsibilities
- unnecessary abstractions
- overengineering
- circular dependencies
- incorrect ownership of responsibilities
- runtime dependency issues
- improper async boundaries
- MCP layer violations
- Coinbase-specific logic leaking into generic layers

Focus on:
- ingestion layer
- registry layer
- pub/sub layer
- snapshot layer
- runtime layer
- MCP layer
- FastAPI lifecycle

Do NOT rewrite the whole project.

Only suggest targeted improvements with reasoning.
Prioritize maintainability, readability, and explainability.
```

---

## Technical Integrity Review

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md

Review the entire codebase for technical integrity.

Check for:
- broken imports
- invalid package structure
- missing dependencies
- incorrect type hints
- async issues
- websocket lifecycle issues
- resource leaks
- queue misuse
- missing cleanup
- concurrency risks
- exception handling gaps
- stale state issues
- reconnect handling problems
- FastAPI lifecycle issues
- test inconsistencies

Suggest only targeted fixes.
Do not massively refactor the project.
```

---

## Documentation & LLM Usability Review

```text
Read:
- docs/SYSTEM_OVERVIEW.md
- docs/SYSTEM_ARCHITECTURE.md
- docs/MCP_CONTEXT.md
- docs/TOPICS.md
- app/mcp/tools.py

Review the project from the perspective of an LLM agent and a new engineer.

Check for:
- unclear MCP tool descriptions
- ambiguous topic schemas
- inconsistent naming
- missing example payloads
- undocumented failure modes
- unclear API behavior
- unclear subscription behavior
- missing reconnect explanations
- missing stale state explanations
- unclear walkthrough flow

Determine whether:
- a new engineer could understand the project quickly
- an LLM agent could use the MCP tools correctly on first attempt

Suggest targeted documentation improvements only.
```

---

# Final Reminder

After each file generation prompt, tell Cursor:

```text
Do not modify other files unless absolutely necessary.
```