# TradingOS

TradingOS is a **practice-first trading operations foundation**. It separates the operator-facing Nuxt control plane from a FastAPI service that owns persistence, policy state, audit records, and future broker orchestration.

> The scaffold does not accept broker credentials, connect to IQ Option, generate live trade signals, or place orders. `REAL` mode is deliberately hard-disabled in the API.

| Directory | Responsibility |
|---|---|
| `frontend/` | Nuxt application for operational visibility and configuration. |
| `backend/` | FastAPI service, SQLAlchemy models, risk defaults, audit feed, and broker-adapter boundary. |
| `docs/` | Architecture and source-inspection findings. |

## Local development

See [`docs/LOCAL_RUNBOOK.md`](docs/LOCAL_RUNBOOK.md) for the local startup sequence, encrypted credential setup, health checks, and the migration contract for a later VDS deployment.

The Nuxt frontend uses **Bun**. From `frontend/`, run `bun install`, `bun run dev`, `bun run typecheck`, and `bun run build`. The FastAPI backend remains Python-based and is installed separately from `backend/`.

## Safety boundary

The API initializes in `PRACTICE` mode, persists risk policy defaults, and makes a real-mode request return `403`. Its local broker adapter verifies `PRACTICE` after connecting, persists only encrypted credentials, and starts with practice execution disabled. Any future order attempt must pass an approved, persisted order-intent workflow after deterministic risk authorization and reconciliation.
