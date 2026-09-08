# TradingOS End-to-End Delivery Tasks

- [x] Build a single-writer, practice-account iqair worker with encrypted configuration and connection-state tracking.
- [x] Persist normalized account snapshots, asset metadata, candles, positions, orders, trade outcomes, and reconciliation events through SQLAlchemy.
- [x] Implement market-data freshness detection, restart recovery, and fail-closed halting behaviour.
- [x] Add deterministic feature engineering, strategy schemas, walk-forward validation, and versioned learning episodes.
- [x] Integrate aircore/airpy through bounded workflows, typed research outputs, model budgets, and MindGraph-based context compression.
- [x] Implement an idempotent order-intent ledger, deterministic risk authorization, and a practice-only execution adapter.
- [x] Extend the Nuxt control plane for configuration, research runs, validation reports, execution state, risk events, and audit records.
- [x] Add contract, unit, integration, restart, and failure-injection tests across the full service boundary.
- [x] Document operations, credential handling, data retention, live-mode gates, and the remaining broker-hardening requirements.
- [x] Package local startup, shutdown, configuration, and health-check scripts for the user’s always-on computer.
- [x] Provide a deployment-neutral configuration contract and a tested migration guide for moving the same services to a VDS.
- [x] Make the Nuxt frontend Bun-compatible, resolve the Vue Router/Volar type-check mismatch, and add missing browser static assets.
- [x] Add an explicit local credential-configuration and PRACTICE-only connection walkthrough to the control plane and runbook.
- [x] Add a local login page for admin-token authentication, encrypted IQ Option credential submission, and guided PRACTICE-only connection and reconciliation.
- [x] Wire the full strategy → intent → practice-execution loop with a fail-closed guard, idempotent per-candle intents, and audit coverage.
- [x] Ship the operator UI increments: strategy desk with walk-forward history, outcome analytics with per-symbol drill-down, strategy comparison, candle chart with signal markers, WebSocket live loop panel, remote-access gate with TOTP provisioning QR, and a backtest lab whose sweep picks become draft strategies with recomputed evidence.
- [x] Add per-strategy evidence export (CSV/PDF), bounded sweep-parameter memory so picks recall their cell, and deduplicated alerting when a loop guard trips.
