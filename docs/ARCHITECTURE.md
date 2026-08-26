# TradingOS Service Architecture

TradingOS runs as two independently deployable services. The Nuxt frontend is a read-first control plane. FastAPI is the security and data boundary; its SQLAlchemy models persist account configuration, watchlists, risk policy, strategy versions, order intents, orders, outcomes, and audit events.

> **Hard boundary:** Models and frontend clients can create a research or configuration request. They cannot submit a broker command. The broker worker must receive only a deterministic `ApprovedOrder` after persistence, risk evaluation, and reconciliation.

| Component | Owns | Must not own |
|---|---|---|
| Nuxt frontend | Visual state, operator navigation, authenticated configuration requests | Credentials, risk authority, direct broker calls, real-mode bypasses |
| FastAPI gateway | API validation, persisted state, audit, health and mode boundaries | Direct LLM authority over order submission |
| Risk service | Deterministic sizing and rejection reasons | Model-generated overrides |
| Broker adapter | A future isolated single-writer connection to iqair | UI state, strategy synthesis, persistence authority |
| SQLAlchemy store | Immutable or append-oriented execution records | Raw credentials or unbounded raw market payloads |

The initial vertical slice is intentionally **practice-only**. Its mode endpoint returns a hard failure for real execution, its broker adapter raises `BrokerExecutionDisabled`, and its system state starts paused. This makes the backend useful for schema, API, UX, and audit development without pretending that a real trading path exists.
