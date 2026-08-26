# TradingOS Foundation

**Author:** Manus AI
**Reference date:** 26 August 2026
**Status:** Architecture baseline for implementation

> **Finance and trading disclaimer:** I am an AI, not a licensed financial advisor. This document is engineering analysis and system design, not guaranteed trading advice; investing and automated trading carry risks that the account owner bears.

## Executive summary

TradingOS is designed as an autonomous trading operating system for a single human-owned IQ Option account. Its core loop is: observe market and account state, generate candidate hypotheses, validate them against deterministic data and historical simulations, apply policy and risk gates, execute only an admissible action, observe the resulting lifecycle, and record the outcome for later evaluation. The system may run unattended after the account owner explicitly enables a mode, but it must be **fail-closed**: when state is stale, account mode is ambiguous, a risk limit is breached, a broker response is uncertain, or the process loses connectivity, it stops opening new exposure and reconciles the account before resuming.

The inspected codebases provide a strong foundation, but not a complete trading OS. `aircore` is an execution runtime with provider-agnostic workflows, capabilities, policy limits, retries, consensus, journaling, checkpointing, subprocess sandboxing, event observation, and MindGraph context compression. `airpy` adds model-provider adapters, structured output, tool calling, sessions, and provider usage reporting. `airlang` provides a declarative lexer/parser/IR/executor that lowers workflow definitions into `aircore` and `airpy`. `airclii` exposes command-line execution and trace rendering. `iqair` is an unofficial IQ Option websocket wrapper with a high-level client, an object-oriented broker façade, event streams, account switching, asset discovery, position/history recovery, and mode-specific order/close methods.

The most important architectural decision is to keep **reasoning separate from authority**. LLM-based agents can research, propose, compare, and explain strategies. They must not be the final authority on position size, account mode, maximum loss, duplicate-order prevention, or whether a broker response is sufficiently confirmed. Those decisions belong to deterministic services and policy gates implemented in FastAPI. The model can produce a typed `TradeIntent`; a risk engine either rejects it or converts it into a bounded `ApprovedOrder`; only the execution gateway can submit that order to iqair.

## What was actually inspected

The repositories were cloned and inspected at source level, including runtime modules, provider code, broker code, websocket dispatch, streaming, agent adapters, specifications, tests, and package metadata. The nested `iqair-1.0.0/iqair` source tree is byte-for-byte equivalent to the root `iqair/` package in the checked-out repository, so it does not represent a second implementation that needs separate integration.

| Area | Inspected implementation | Practical conclusion |
|---|---|---|
| Workflow runtime | `aircore/workflow.py`, `scheduler.py`, `policy.py`, `tools.py`, `consensus.py`, `parallel.py` | Deterministic declared structure, sequential or concurrent groups, fan-in, capability and policy checks, retries, approvals, and runtime/cost checks. |
| Audit and recovery | `journal.py`, `events.py`, `observability.py`, `checkpoint.py`, `graph.py` | Event-driven journal and metrics are useful for audit; checkpoint replay is position-indexed and only covers sequential JSON-safe steps. |
| Context and memory | `memory.py`, `persistent_memory.py`, `mindgraph.py`, `airpy/model_agent.py` | MindGraph summarizes results, supports bounded context neighborhoods, expands exact values on demand, deduplicates repeated read calls, and compacts old nodes. |
| Model execution | `airpy/providers.py`, `model_agent.py`, `judge_consensus.py`, `structured_output.py`, `litellm_provider.py`, `openai_provider.py` | Provider adapters normalize requests/responses; typed outputs and judge-based consensus are available, but internal tool calls bypass the aircore scheduler. |
| Declarative execution | `airlang/lexer.py`, `parser.py`, `bindings.py`, `executor.py`, `airlang-spec-v1.md` | AirLang can describe a bounded workflow and compile it to runtime objects; it is not a dynamic autonomous planner. |
| IQ Option account and orders | `iqair/client.py`, `broker.py`, `api.py`, `ws/client.py` | Six mode families are represented: turbo, binary, digital, forex, crypto, and CFD. Modern placement and close paths are mode-specific. |
| Market/account streams | `iqair/streaming.py`, `models.py` | Candles and position changes are event-driven; balance, payout, and asset status are polling wrappers; news is explicitly unimplemented. |
| LLM tool surface | `iqair/agent/tools.py`, `dispatcher.py`, `schemas.py`, `server.py` | JSON-safe tools exist, but the built-in HTTP server has no authentication and one global session, so it must not be exposed as the TradingOS security boundary. |
| Validation | `aircore/tests`, `iqair/tests`, package metadata | Aircore passes 449 tests and skips one optional test in this environment; iqair passes 8 structural tests and skips 47 credential/live-backend tests without account credentials. |

The repository-level documentation is useful but partly stale relative to the code. For example, the actual `aircore/approval.py` and scheduler implement a synchronous approval callback, while older architecture prose describes approval as not built. TradingOS therefore treats the **implementation and tests as authoritative**, and uses the specifications as design history rather than as a complete API contract.

## Reusable capabilities and hard limitations

### aircore and airpy

`aircore` exposes an `Executable` contract. A `Tool` and an `airpy.ModelAgent` can occupy the same workflow positions because the scheduler calls `execute()` and optionally reads `usage()`. This is a good seam for TradingOS: market-data adapters, feature calculations, backtests, risk checks, model calls, and broker actions can all be represented as typed workflow steps while the runtime records the execution trace.

`Policy` currently enforces `require_agent`, `max_parallel`, `max_runtime`, `max_cost`, and `approval_for`. Capability checks compare tool requirements with the capabilities of an attached `aircore.Agent`. This is helpful but not enough for trading authority because internal tool calls made inside `ModelAgent(tools=[...])` do not pass through the scheduler. TradingOS must therefore avoid placing broker-side effects inside an unrestricted model tool list. A model may call read-only research tools, but order submission must be a FastAPI service method reached only after deterministic authorization and risk evaluation.

`MindGraph` is directly relevant to the user's token-consumption requirement. It replaces repeated raw results with compact summaries, keeps a `full_ref`, exposes `expand_node`, and supports shared-graph deduplication across model agents. Structured numeric and OHLC data can be summarized without another LLM call. Free text still requires either a deterministic truncation or a cheap summarizer. TradingOS should use deterministic summaries for candles, indicators, account snapshots, risk metrics, and trade outcomes, and reserve model calls for regime interpretation, strategy synthesis, and post-trade reflection.

The runtime's checkpointing should not be mistaken for durable trading recovery. It skips previously succeeded sequential steps by position and tool name, but parallel/consensus groups rerun in full, outputs must be JSON serializable, and the store rewrites a file without cross-process locking. TradingOS needs its own database-backed order-intent ledger and idempotency keys. A broker request must be reconciled from account state before any retry can be considered safe.

### iqair

The high-level IQ Option client connects over websocket, selects a balance mode, resolves current asset metadata, obtains candles, and provides mode-specific trade paths. For turbo and binary, `buy()` resolves a live active ID and may retry once when a purchase window closes. Digital options resolve a live server-issued instrument rather than constructing a symbol client-side, require a remaining-expiration margin, and use a position lookup before early close. Forex, crypto, and CFD use distinct namespaced market-order endpoints and treat `amount` as margin, with leverage required.

Asset discovery is dynamic. `get_asset_metadata()` combines turbo/binary initialization, digital underlying lists, and marginal instrument lists into a unified directory containing IDs, open state, schedules, precision, payout where available, and category. TradingOS must revalidate this directory immediately before order authorization; static asset IDs and stale open/closed assumptions are not acceptable.

Position identity is non-uniform. For turbo/binary, `external_id` may equal the order ID, while digital and margin positions can use `external_id` for the position ID and carry the originating order IDs inside nested `raw_event` data. The wrapper provides `position_matches_order_id()` and mode-aware recovery methods. TradingOS should use those helpers rather than equality checks and should persist both broker order IDs and resolved position IDs when known.

`iqair.streaming` offers subscriptions and callback handles. Candle and position streams are true websocket-derived feeds. Price is derived from candle updates, not a dedicated confirmed tick endpoint. Balance, payout, and asset-status streams poll at intervals. Connection events currently include connected, disconnected, and auth-failed; reconnecting is defined but not emitted centrally. The TradingOS supervisor must add its own connection state machine, stale-data timers, and post-reconnect reconciliation.

The wrapper disables TLS certificate verification in its low-level API session and websocket startup. This is a major production-hardening concern. TradingOS should isolate iqair in a broker worker, restrict its network egress at the process/container level where possible, record this limitation explicitly, and consider adding a hardened transport configuration before real-account use. The system must never log account passwords, session cookies, websocket payloads containing secrets, or raw credentials.

## Proposed system architecture

TradingOS uses a Nuxt frontend for control, inspection, and observability, and a FastAPI backend for authenticated API access, orchestration, persistence, background workers, and broker isolation. Nuxt's current server directory convention automatically registers server handlers, but because the requested architecture explicitly uses FastAPI, Nuxt server routes should remain a thin same-origin proxy or be disabled for business logic. The backend is the authority for credentials, account mode, risk policy, strategy state, and order execution. Nuxt only receives redacted state and submits authenticated commands to FastAPI. Nuxt's runtime configuration must keep secrets server-side, consistent with the framework's runtime configuration guidance [3].

FastAPI should run as the API process and expose a separate worker lifecycle for broker connectivity, market ingestion, strategy evaluation, and reconciliation. FastAPI's deployment guidance emphasizes HTTPS, startup behavior, crash restarts, process replication, and memory per process [4]. TradingOS must not run multiple independent broker workers against one account without a single-writer lease; duplicate websocket sessions and competing order loops would make the account state ambiguous.

```text
                         +-----------------------------+
                         |       Nuxt Control Plane    |
                         | dashboard / setup / audit   |
                         +--------------+--------------+
                                        |
                              authenticated HTTPS API
                                        |
                         +--------------v--------------+
                         |        FastAPI Gateway       |
                         | auth, commands, redaction   |
                         +------+----------------+-----+
                                |                |
                    +-----------v----+   +-----v----------------+
                    | TradingOS       |   | Persistent Store     |
                    | Supervisor      |   | PostgreSQL/SQLite*   |
                    | state machine   |   | intents, fills,     |
                    +---+---------+---+   | candles, policies,   |
                        |         |       | journals, metrics    |
          +-------------v-+   +---v----------------+
          | Market Worker  |   | Strategy/Risk Loop |
          | iqair streams  |   | aircore + airpy    |
          | + polling      |   | typed proposals    |
          +-------+--------+   +----------+----------+
                  |                         |
                  +-------------+-----------+
                                |
                       deterministic order gate
                                |
                         +------v------+
                         | Broker Worker|
                         | iqair client |
                         | single writer|
                         +------+-------+
                                |
                         IQ Option account
```

`*` SQLite is suitable for a first local single-process development slice; a production deployment should use a transactional database with advisory locking or an equivalent single-writer lease. The account worker should be independently restartable and reconstruct its state from persisted intents plus fresh broker snapshots.

## Domain model

TradingOS should store both raw evidence and compact derived records. Raw broker responses belong in encrypted or access-controlled storage with retention limits. The model-facing context should receive compact, versioned summaries and references rather than raw payloads.

| Entity | Purpose | Key invariants |
|---|---|---|
| `AccountProfile` | One human-owned account and current operating mode | Credentials never enter model prompts; only one active broker writer; mode is explicitly `PRACTICE` or `REAL`. |
| `Watchlist` | User-selected ticker/category/timeframe set | Each symbol is checked against current asset metadata and open schedule before use. |
| `MarketSnapshot` | Candle, derived price, payout, and status data | Carries source timestamp, server timestamp, freshness, category, and schema version. |
| `FeatureSnapshot` | Deterministic indicators and regime features | Reproducible from stored market data and feature version. |
| `StrategyVersion` | Immutable strategy artifact and evaluation results | No live activation without validation and policy approval. |
| `TradeIntent` | Model or deterministic strategy proposal | Contains thesis, side, entry context, expiry/exit plan, confidence, and evidence references; cannot directly place an order. |
| `RiskDecision` | Deterministic accept/reject/resize decision | Includes balance snapshot, exposure, daily loss, sizing formula, and policy version. |
| `ApprovedOrder` | Fully bounded broker command | Has idempotency key, account mode, asset metadata version, amount/margin, leverage, and expiry constraints. |
| `OrderRecord` | Broker submission and reconciliation ledger | State transitions are append-only and broker IDs are never overwritten. |
| `TradeOutcome` | Settled P&L and context | Records win/loss/expiry/close reason, realized P&L, fees/payout, and strategy version. |
| `LearningEpisode` | Post-trade evidence for strategy evaluation | Learning updates are versioned and cannot silently mutate a live strategy. |
| `SystemEvent` | Operational audit and alerts | Includes connection, stale-data, mode-switch, risk halt, and reconciliation events. |

## Autonomous loop

The system should not ask a model to invent an arbitrary runtime workflow on every tick. The workflow is declared by the application and the model fills bounded decision slots. This follows the inspected aircore design, where structure is developer-declared and consensus strategies reduce outputs rather than rewriting the execution graph.

1. **Ingest.** Subscribe to candles and position changes for the enabled watchlist. Poll balance, payout, and asset status on slower cadences. Attach source and server timestamps.
2. **Normalize.** Convert iqair dataclasses/raw messages into TradingOS domain records. Detect stale, missing, contradictory, or out-of-order data.
3. **Compute.** Calculate deterministic features, rolling volatility, trend, momentum, spread proxies, payout, exposure, drawdown, and regime labels.
4. **Research.** Use low-cost deterministic screens first. Invoke a model only when the snapshot changes materially, a scheduled research window opens, or a strategy needs interpretation. Use MindGraph to preserve compact evidence and deduplicate repeated reads.
5. **Propose.** One or more specialists produce typed `TradeIntent` objects. Candidate specialists may include technical, regime, risk-context, and execution-quality agents. They share a MindGraph but must not share mutable broker authority.
6. **Validate.** Re-run deterministic checks, perform a no-lookahead backtest or walk-forward evaluation when the intent changes a strategy, and require agreement or a confidence threshold for ambiguous proposals. The validator must reject if the information cutoff and evaluation window overlap.
7. **Authorize.** The deterministic risk engine checks account mode, open positions, maximum concurrent trades, per-trade risk, daily loss, drawdown, payout, schedule, cooldowns, duplicate intent keys, and broker health. It can reject or resize; it cannot be overridden by a model.
8. **Execute.** The single broker worker sends only an `ApprovedOrder`. It persists the intent before submission, records the broker response, and never blindly retries a side-effecting submission.
9. **Monitor.** Use trade updates and periodic position queries to reconcile `OPENED`, `SOLD`, `WON`, `LOST`, `EXPIRED`, or `UNKNOWN`. An `UNKNOWN` state freezes new entries until reconciled.
10. **Learn.** After settlement, compute realized outcome, slippage/latency proxy, market regime, thesis consistency, and failure tags. Update research memory and evaluation datasets, not the live strategy in place.
11. **Compact.** Collapse old MindGraph nodes and aggregate cycle outcomes so an always-on process maintains bounded prompt size and bounded in-memory state.

## Account modes and the real-account boundary

The default mode is `PRACTICE`. A process start, account connection, or strategy validation must never silently select `REAL`. Account mode should be part of every command, journal record, order intent, risk decision, and UI status. A `REAL` session must require an explicit account-owner action to enable it in the application, followed by a visible preflight showing the account identifier, balance mode, active risk policy, watchlist, maximum loss, and whether the strategy version has passed validation. The application can then run unattended within those bounds; this is not a request for the system to conceal or bypass the owner’s control.

Automatic switching to `PRACTICE` is appropriate for strategy validation only when no live position or pending order could be affected and the switch is recorded. Automatic switching away from `REAL` should be treated as a protective halt and reconciled against the broker after reconnect. Because the iqair wrapper holds one global client/session in its optional agent layer, TradingOS should manage a dedicated `IQOptionClient` instance in the broker worker and expose only a narrow internal adapter.

The live execution path should have an independent kill switch. Setting `TRADING_ENABLED=false`, losing the single-writer lease, receiving stale market data, exceeding a loss threshold, or detecting account-mode drift must prevent new orders. Existing positions may still be monitored and closed according to a deterministic protective policy, subject to broker availability and the owner’s configured limits.

## Risk-engine baseline

The risk engine is intentionally conservative and deterministic. The initial implementation should support fixed limits rather than attempting to discover optimal risk from sparse evidence. Strategy research may propose parameters, but activation requires an immutable policy snapshot.

| Control | Initial behavior |
|---|---|
| Account mode | Practice by default; real mode disabled until explicit enablement. |
| Per-trade risk | Fixed fraction of current balance, capped by an absolute dollar ceiling. |
| Daily loss | Hard stop for new entries after the configured realized-plus-mark-to-market threshold. |
| Drawdown | Hard stop or practice-only mode after maximum peak-to-trough drawdown. |
| Concurrent exposure | Limit by number of positions, total margin, and correlated watchlist exposure. |
| Duplicate prevention | Idempotency key derived from strategy version, asset, timeframe, decision timestamp bucket, and side. |
| Data freshness | Reject entry when the latest candle/quote or asset status exceeds its freshness budget. |
| Broker uncertainty | Freeze new entries when an order response or position match is unknown. |
| Digital expiration | Preserve iqair’s minimum remaining-expiration margin and prefer longer periods until separately validated. |
| Margin leverage | Require instrument-specific leverage validation; never assume a fixed leverage is valid for every CFD/forex/crypto asset. |
| Recovery | Reconcile open positions/history after every reconnect and process restart before enabling entries. |

## Strategy development and learning

TradingOS should distinguish **research**, **validation**, **activation**, and **live adaptation**. A model can generate a candidate strategy specification containing feature definitions, entry/exit rules, timeframes, assumptions, and failure conditions. Deterministic code compiles that specification into a testable strategy object. The candidate is evaluated on a time-ordered dataset with a censor gap and no future leakage. Walk-forward or rolling validation should be the default for any strategy intended to run live.

A losing trade is not automatically evidence that the strategy is wrong. The learning episode should classify whether the trade followed the strategy, whether the data were fresh, whether execution was accepted as intended, what regime was active, whether the loss was within the expected distribution, and whether the thesis was invalidated. Updates should be written as new strategy versions or parameter proposals with provenance. The system must not let a model silently self-modify the live execution code, risk limits, or broker adapter.

For consensus, aircore's `ConsensusGroup` is appropriate for multiple independent specialist proposals, and `airpy.JudgeConsensus` can synthesize or select typed output. However, a consensus agreement is not a risk approval. The final `RiskDecision` remains deterministic. The system should prefer exact structured outputs over free text and capture the judge metadata, model, confidence, and reasoning in the journal.

## Token-consumption strategy

Token cost is reduced by making the market-data path mostly deterministic and by treating LLM calls as event-driven exceptions rather than per-candle obligations. The following policy is recommended.

| Layer | Token policy |
|---|---|
| Candle ingestion | No LLM. Store normalized candles and compute indicators in Python. |
| Feature summaries | No LLM. Use deterministic numeric/ohlc summarizers and compact JSON. |
| Repeated reads | Use MindGraph deduplication only for read-only, time-stable calls; disable it for live prices and side effects. |
| Per-cycle context | Send a bounded MindGraph neighborhood, not full history. |
| Model frequency | Trigger on regime change, scheduled review, strategy version change, or a material risk event, not every tick. |
| Specialist fan-out | Parallelize a small fixed number of agents and share one MindGraph to avoid duplicate market-data tool calls. |
| Long-running memory | Compact old cycle nodes into aggregate summaries and persist full evidence by reference. |
| Model tiers | Use a small/cheap model for extraction, classification, and summarization; reserve a stronger model for strategy synthesis or ambiguous regime decisions. |
| Output size | Enforce typed schemas, concise evidence references, and bounded reasoning fields. |
| Failure path | Fall back to deterministic no-trade or existing validated strategy; never spend additional tokens in a runaway loop. |

The inspected `ModelAgent` already provides `use_mindgraph`, deterministic structured summarization, shared-graph deduplication, `max_turns`, structured outputs, and provider usage reporting. TradingOS should expose savings metrics in the dashboard and enforce per-cycle and per-day model budgets outside aircore, because model-internal tool calls are not individually visible to the scheduler.

## API surface for the first implementation

The initial FastAPI surface should be narrow and read-heavy. The command endpoints must be authenticated, audited, and rate-limited. Live-order submission should not be exposed as a generic `call_tool` endpoint.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Process, worker, broker, database, and data-freshness health. |
| `GET` | `/api/v1/state` | Redacted current system state, account mode, watchlist, risk status, and last cycle. |
| `GET` | `/api/v1/assets` | Current iqair asset directory with open state and payout where available. |
| `PUT` | `/api/v1/watchlist` | Replace the user-selected watchlist and timeframes. |
| `GET` | `/api/v1/strategies` | List immutable strategy versions and validation status. |
| `POST` | `/api/v1/strategies/research` | Start a bounded research run; returns a run ID. |
| `POST` | `/api/v1/mode/enable-real` | Explicitly enable real mode after preflight; never called by the model. |
| `POST` | `/api/v1/mode/disable-real` | Protective disable and reconciliation. |
| `POST` | `/api/v1/system/pause` | Prevent new entries while continuing reconciliation. |
| `POST` | `/api/v1/system/resume` | Resume only if all health/risk checks pass. |
| `GET` | `/api/v1/orders` | Order ledger with redacted broker details. |
| `GET` | `/api/v1/trades` | Settled outcomes, P&L, strategy version, and learning tags. |
| `GET` | `/api/v1/events` | Audit feed and operational events. |
| `GET` | `/api/v1/stream` | Server-sent events for dashboard updates, if needed. |

## Implementation roadmap

### Phase A: Safe foundation

Create the Nuxt and FastAPI repositories, configuration, typed domain models, local SQLite schema, authenticated development session, redaction utilities, health endpoints, and a practice-only simulated broker. Add a dashboard showing system state, watchlist, risk limits, recent trades, and event history. No real IQ Option credentials are required for this phase.

### Phase B: Read-only iqair adapter

Wrap `IQOptionClient` behind an interface with explicit connect, disconnect, balance, asset discovery, candle subscription, position snapshots, history, and connection events. Implement stale-data detection, reconnect handling, credential redaction, and a single-writer worker. Add contract tests using fake client objects and recorded normalized payloads. Do not enable order placement yet.

### Phase C: Deterministic research and backtesting

Implement feature calculation, a strategy schema, walk-forward backtesting with censor gaps, validation reports, strategy versioning, and a practice-only execution simulator. Use aircore workflows for research stages and journal every run. Add MindGraph context and savings measurements.

### Phase D: Practice execution

Add a bounded practice broker adapter using iqair's modern mode-specific methods. Persist intent-before-submit, order IDs, broker responses, position matches, state transitions, and reconciliation. Exercise all supported paths only against practice balance and only with explicit test flags. Never reuse the old dead `buy_order()` or legacy close paths.

### Phase E: Controlled real-mode gate

Add explicit owner enablement, account-mode preflight, maximum-loss confirmation, kill switch, live audit events, single-writer locking, and an automated rollback to no-new-entries on uncertainty. Keep this phase disabled by default in configuration and require the account owner to enable it separately after practice validation.

### Phase F: Learning and operations

Add post-trade learning episodes, strategy proposals, drift detection, bounded self-review, dashboards for token usage and risk, alerting, backups, and process-supervisor deployment. Live strategy activation remains versioned and policy-gated; no autonomous code mutation is allowed.

## Testing and acceptance gates

Every phase must pass offline tests before any broker test. The project should include unit tests for risk mathematics, idempotency, state transitions, asset freshness, mode switching, position/order matching, schema validation, MindGraph compaction, token budgets, and failure-closed behavior. Contract tests should replay recorded iqair responses without credentials. Practice-backend tests should be opt-in and isolated from real credentials. Live tests, if ever run, must be explicit, limited, and independently reviewable.

A release is not ready for unattended practice operation until it can restart, reconnect, recover open positions/history, avoid duplicate orders, preserve an append-only audit trail, and stop opening new exposure under every injected uncertainty condition. A release is not ready for real mode until the owner has separately validated the account, broker behavior, risk limits, and legal/compliance implications.

## Source references

This foundation is based on direct source inspection plus the current official framework documentation.

1. [Omerhrr/aircore — provider-agnostic AI execution runtime and integrated airpy/airlang/aircli source](https://github.com/Omerhrr/aircore)
2. [Omerhrr/iqair — unofficial IQ Option websocket trading wrapper](https://github.com/Omerhrr/iqair)
3. [Nuxt v4 server directory and server-handler documentation](https://nuxt.com/docs/4.x/directory-structure/server)
4. [FastAPI deployment concepts](https://fastapi.tiangolo.com/deployment/concepts/)

## Basis, time, assumptions, and compliance disclosure

**Basis:** The design treats balance, positions, order states, realized P&L, asset open state, and payout as broker-sourced observations. Risk controls are deterministic policy inputs; model confidence is advisory evidence rather than a substitute for risk authorization. **Time:** Repository and official-documentation observations were collected on 26 August 2026; live IQ Option account data were not accessed in this task. **Assumptions:** The first deployment is single-account, single-writer, practice-first, with local SQLite during development and a transactional database before serious unattended operation. **Sources and confidence:** Code-level conclusions are high-confidence for the checked-out commits and tests; broker behavior remains subject to undocumented IQ Option API drift, and several iqair transport details require further hardening before production. **Compliance:** This is research and analysis only, not personalized financial advice.
