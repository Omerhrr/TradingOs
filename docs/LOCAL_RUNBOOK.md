# Local-First Operations and VDS Migration

TradingOS is designed to run first on the operator’s own computer. Both services bind to `127.0.0.1` in the provided startup script, so the control plane and broker credentials are not exposed on the local network by default.

## Local prerequisites

Install Python 3.11 or newer, Node.js 22 or newer, and Bun 1.3 or newer. From `backend/`, install the package and development dependencies with `pip install -e '.[dev]'`. From `frontend/`, run `bun install`, then use `bun run dev`, `bun run typecheck`, or `bun run build` as needed. The first broker connection is always PRACTICE-only and does not place an order.

Generate an encryption key with `python scripts/generate_encryption_key.py`. Create `backend/.env` locally—do not commit it—with the following values:

```text
TRADINGOS_DATABASE_URL=sqlite:///./data/tradingos.db
TRADINGOS_CORS_ORIGINS=http://127.0.0.1:3001,http://localhost:3001
TRADINGOS_LOCAL_ADMIN_TOKEN=replace-with-a-long-random-local-token
TRADINGOS_CREDENTIAL_ENCRYPTION_KEY=the-generated-fernet-key
TRADINGOS_AUTO_RECONCILE_ENABLED=false
TRADINGOS_PRACTICE_EXECUTION_ENABLED=false
TRADINGOS_REAL_EXECUTION_ENABLED=false
TRADINGOS_TOTP_REQUIRED=false
```

Run `bash scripts/start_local.sh` from the repository root. Verify the service using `python scripts/healthcheck.py` and `python scripts/verify_practice_boundary.py`. The FastAPI interactive API documentation is then available at `http://127.0.0.1:8000/docs`, and the Nuxt control plane is at `http://127.0.0.1:3001`.

After encrypted credentials have been stored and a manual PRACTICE connection and reconciliation have succeeded, setting `TRADINGOS_AUTO_RECONCILE_ENABLED=true` starts a single local background worker. It maintains one practice-broker session and reconciles on the configured interval. On a background connection or reconciliation error, the worker moves the account to `HALTED`, disconnects the broker, and does not resume on its own. The loop starts disabled by default.

> The credential endpoint requires `X-TradingOS-Token` and stores only Fernet-encrypted values in the local SQLAlchemy database. Keep the encryption key and local admin token outside the repository and password manager-sync them as separate secrets.

## Backtest lab and two-factor login

The **Backtest lab** (`/backtest` in the control plane) runs read-only walk-forward previews over candles the broker worker already stored: one run returns headline metrics, the multiplicative equity curve, and every simulated trade, and the sweep draws a bounded fast × slow EMA grid (24 cells max per request). The runner never persists an evaluation, changes strategy status, writes audit rows, or touches the broker.

A promising sweep cell can be saved straight from the lab: run the pick, name it, and press **SAVE AS DRAFT STRATEGY** (`POST /api/v1/strategies/from-sweep`). The desk never trusts browser-echoed numbers — the server re-runs the walk-forward over the candles stored at save time and persists that as the draft's evidence, and the draft starts with the standard 0.05 max-drawdown gate. If candles arrived between the preview and the save, the UI flags the drift. The draft still earns VALIDATED only through the Strategy desk's persisted evaluation.

For remote deployments the interactive sign-in can demand a second factor. Set `TRADINGOS_TOTP_REQUIRED=true`, then provision a secret from the Local setup page (`POST /api/v1/auth/totp/provision`): the base32 secret, its `otpauth://` URI, and a scanning QR code are shown exactly once — scan the QR into any TOTP authenticator app (or type the secret manually) and the 6-digit code is demanded at `POST /auth/login` from then on. Wrong codes count toward the per-IP lockout, provisioning is audited, and the plaintext secret never reaches the database, logs, or audit ledger. The QR encodes the provisioning URI, so a screenshot of it is exactly as sensitive as the secret itself. The admin-token header/bearer paths are machine credentials and remain code-free; a required-but-disabled secret fails closed.

## Sweep memory, evidence export, and guard alerting

The lab keeps a bounded memory of the parameter surfaces it sweeps: every `POST /api/v1/backtest/sweep` response carries a `sweep_run_id`, the most recent 24 surfaces stay listed at `GET /api/v1/backtest/sweeps` (the "Sweep memory" card on `/backtest` can re-open any of them into the heatmap), and cells already promoted to drafts are marked with a dot per surface signature via `GET /api/v1/backtest/picks`. A saved pick remembers its exact (fast × slow) cell in `SweepPickRecord` (`GET /api/v1/strategies/{id}/sweep-history`), and the Strategy desk shows the provenance as a `LAB CELL f/s · SYMBOL` chip.

Every strategy version can export its evidence as a report of record: `GET /api/v1/strategies/{id}/evidence` is the JSON bundle (identity, definition, latest persisted evaluation, lab provenance, gate activity, and the settled practice-trade ledger), `GET /api/v1/strategies/{id}/evidence/export.csv` renders the same truth as a sectioned CSV, and `.../export.pdf` as a compact PDF report. The Strategy desk drives both with `EXPORT CSV` / `EXPORT PDF` buttons; downloads carry the admin token, so they keep working behind the remote-access gate.

Loop guards now speak up. A skipped tick (paused system, missing PRACTICE mode, disconnected broker) raises a `LOOP_GUARD_TRIPPED` alert, a broken tick raises `LOOP_TICK_FAILED`, and a failed order submission raises `LOOP_SUBMIT_ERROR`. Raising is deduplicated inside `TRADINGOS_ALERT_COOLDOWN_SECONDS` (default 60s: one alert grows an `occurrences` counter instead of flooding the ledger), a guard alert auto-resolves the moment a later tick passes its guards, and every raise/ack/resolution is audited. Alerts stream to the UI over the existing WebSocket channel (`alert.raised` / `alert.acknowledged`), surface as a panel and a nav badge on the control plane, and can be POSTed to an external endpoint with `TRADINGOS_ALERT_WEBHOOK_URL` (fire-and-forget, never breaks trading ops).

## Alert center, webhook retry policy, and alert rules

Alerting graduated from a fire-and-forget webhook POST to an operable subsystem with its own page (`/alerts`):

* **Webhook retry policy.** Every notification is a persisted `WebhookDelivery` row enqueued in the same transaction as its alert, and a background dispatcher retries with exponential backoff (`TRADINGOS_WEBHOOK_BACKOFF_BASE_SECONDS` * 2^(attempt-1), capped by `TRADINGOS_WEBHOOK_BACKOFF_MAX_SECONDS`) until `TRADINGOS_WEBHOOK_MAX_ATTEMPTS` is reached, then marks the row EXHAUSTED and audits `WEBHOOK_EXHAUSTED`. An operator can reopen any delivery with a fresh attempt budget via `POST /api/v1/alerts/deliveries/{id}/retry` (or the RETRY button on `/alerts`), and `POST /api/v1/alerts/webhook/test` queues a self-contained TEST delivery. When `TRADINGOS_WEBHOOK_SIGNING_SECRET` is set, each POST carries `X-TradingOS-Signature: sha256=<hmac_sha256(secret, body)>` so the receiver can verify authenticity.
* **Alert-rule configuration.** Each known code (LOOP_GUARD_TRIPPED, LOOP_TICK_FAILED, LOOP_SUBMIT_ERROR) has a persisted rule: enabled/disabled (silenced codes raise nothing at all), severity override, per-rule cooldown override, and a per-rule webhook on/off. Rules are seeded from a manifest at boot, listed at `GET /api/v1/alerts/rules`, and updated with `PUT /api/v1/alerts/rules/{code}` — no restart, no redeploy. Missing rows fall back to the built-in defaults.
* **The /alerts page** ties it together: filterable alert ledger (severity, open/acked, code) with per-row ACK and ACK ALL, the rule editor, and the delivery ledger with attempts, next-retry time, HTTP status, last error, and per-row RETRY. It refreshes live over the WebSocket channel (`alert.*`, `webhook.delivered`, `webhook.exhausted`).

## LLM (DeepSeek) and broker credentials

The optional AI research path already speaks any OpenAI-compatible endpoint through airpy's `OpenAIProvider` — DeepSeek's API is directly compatible (including the JSON-mode response format the structured proposal uses). To connect a DeepSeek key:

```
TRADINGOS_AI_ENABLED=true
TRADINGOS_AI_API_KEY=sk-your-deepseek-key
TRADINGOS_AI_BASE_URL=https://api.deepseek.com
TRADINGOS_AI_MODEL=deepseek-chat
```

Restart the backend, then verify readiness on the Local setup page's "AI RESEARCH" card (`GET /api/v1/ai/status` reports enabled/model/endpoint/key-configured plus today's token and run budget — the key itself never leaves the server). Research remains bounded: daily token budget (`TRADINGOS_AI_DAILY_TOKEN_BUDGET`), daily run limit, prompt truncation, deduped identical requests, and a hypotheses-only output contract with no broker or order access.

IQ Option credentials are stored end-to-end encrypted (Fernet, `TRADINGOS_CREDENTIAL_ENCRYPTION_KEY`) via the Local setup page: enter email + password, press STORE ENCRYPTED CREDENTIALS, then CONNECT TO PRACTICE (the adapter refuses anything that is not explicitly PRACTICE), then RUN RECONCILIATION. The password is cleared from the browser after storage and is only decrypted in-memory at connect time.

## VDS migration contract

The application has no hard-coded hostnames, database paths, or broker configuration in source. A VDS move consists of copying the repository and encrypted database backup, providing the same environment variables through the VDS secret manager, installing the declared Python and Node dependencies, and running the API and frontend under a process supervisor and reverse proxy.

The same encryption key is required to decrypt an existing local credential record. If the key is intentionally changed, delete the stored broker credential and configure it again through the protected local/administrative endpoint. Before enabling the broker worker on a VDS, run the health check and practice-boundary check, verify that the account is still `PRACTICE`, then complete at least one reconciliation with no order route enabled. The included `deployment/systemd/` unit files are reference units for an Ubuntu-like VDS; place the environment file outside the repository, keep the services bound to loopback, and add a TLS reverse proxy with authentication only after local verification.

| Migration check | Required result |
|---|---|
| API health | `status: ok`, `live_execution: false` |
| Broker mode | Explicitly reports `PRACTICE` after connection |
| Database | Account, risk policy, and audit ledger available after restart |
| Reconciliation | A successful no-order run after broker connection and after simulated reconnect |
| Network | API not publicly exposed until authentication, TLS, and reverse-proxy controls are configured |
