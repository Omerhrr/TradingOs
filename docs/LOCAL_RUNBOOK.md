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
```

Run `bash scripts/start_local.sh` from the repository root. Verify the service using `python scripts/healthcheck.py` and `python scripts/verify_practice_boundary.py`. The FastAPI interactive API documentation is then available at `http://127.0.0.1:8000/docs`, and the Nuxt control plane is at `http://127.0.0.1:3001`.

After encrypted credentials have been stored and a manual PRACTICE connection and reconciliation have succeeded, setting `TRADINGOS_AUTO_RECONCILE_ENABLED=true` starts a single local background worker. It maintains one practice-broker session and reconciles on the configured interval. On a background connection or reconciliation error, the worker moves the account to `HALTED`, disconnects the broker, and does not resume on its own. The loop starts disabled by default.

> The credential endpoint requires `X-TradingOS-Token` and stores only Fernet-encrypted values in the local SQLAlchemy database. Keep the encryption key and local admin token outside the repository and password manager-sync them as separate secrets.

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
