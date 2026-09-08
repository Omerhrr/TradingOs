"""Task 8 live smoke: evidence export, sweep memory, guard alerting.

Boots a real uvicorn server against a throwaway database, seeds candles in
process (sharing TRADINGOS_DATABASE_URL with the subprocess), and walks the
three features end to end through HTTP.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from math import sin, tau

BASE = "http://127.0.0.1:8137"
ADMIN = "smoke-admin-token"
SYMBOL = "SMOKESYM"
TF = 60

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}")


def request(path: str, method: str = "GET", token: str | None = ADMIN, payload: dict | None = None, raw: bool = False):
    body = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-TradingOS-Token"] = token
    if payload is not None:
        body = __import__("json").dumps(payload).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = response.read()
            return response.status, data if raw else __import__("json").loads(data or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, __import__("json").loads(exc.read() or b"{}")


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="tradingos-smoke8-")
    env = dict(os.environ)
    env.update({
        "TRADINGOS_DATABASE_URL": f"sqlite:///{tmpdir}/smoke.db",
        "TRADINGOS_LOCAL_ADMIN_TOKEN": ADMIN,
        "TRADINGOS_CREDENTIAL_ENCRYPTION_KEY": __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode(),
        "TRADINGOS_LOOP_ENABLED": "false",
    })
    # Task 7 lesson, learned twice now: the in-process seeder must share the
    # subprocess's environment, or it silently writes the dev database.
    os.environ.update(env)

    # Seed candles + account in process, sharing the same DATABASE_URL.
    sys.path.insert(0, ".")
    import app.models  # noqa: F401
    from app.database import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    from app.models import AccountConfig, Candle, SystemState

    start = datetime(2026, 1, 1, tzinfo=UTC)
    with SessionLocal() as session:
        session.add(AccountConfig(account_label="smoke", mode="PRACTICE", real_execution_enabled=False, system_state=SystemState.PAUSED.value))
        for index in range(240):
            price = 1.10 + 0.0004 * index + 0.004 * sin(tau * index / 40)
            session.add(Candle(
                symbol=SYMBOL, timeframe_seconds=TF,
                open_time=start + timedelta(seconds=TF * index),
                close_time=start + timedelta(seconds=TF * (index + 1)),
                open_price=price, high_price=price * 1.0005, low_price=price * 0.9995,
                close_price=price, volume=1000.0,
            ))
        session.commit()

    server = subprocess.Popen(
        [".venv/bin/uvicorn", "app.main:app", "--port", "8137", "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            try:
                status, _ = request("/api/v1/health", token=None)
                if status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("server never became healthy")

        # --- 1. Sweep + cell memory ------------------------------------
        status, sweep = request("/api/v1/backtest/sweep", "POST", payload={
            "symbol": SYMBOL, "timeframe_seconds": TF, "censor_gap_seconds": 60,
            "fast_windows": [4, 8], "slow_windows": [24, 34], "volatility_window": 20,
        })
        check("sweep runs", status == 200, str(status))
        check("sweep response carries surface id", isinstance(sweep.get("sweep_run_id"), int), str(sweep.get("sweep_run_id")))

        status, save = request("/api/v1/strategies/from-sweep", "POST", payload={
            "strategy_key": "smoke-lab-pick", "version": "v1", "symbol": SYMBOL,
            "timeframe_seconds": TF, "censor_gap_seconds": 60,
            "fast_window": 8, "slow_window": 34, "volatility_window": 20,
            "sweep_run_id": sweep["sweep_run_id"],
        })
        check("pick saved as draft", status == 200, f"{status} {save}")
        pick = save.get("sweep_pick") or {}
        strategy_id = save["strategy"]["id"]
        check("pick remembers its cell", pick.get("fast_window") == 8 and pick.get("slow_window") == 34 and pick.get("sweep_run_id") == sweep["sweep_run_id"], str(pick))

        status, history = request(f"/api/v1/strategies/{strategy_id}/sweep-history")
        check("strategy sweep history answers", status == 200 and len(history) == 1 and history[0]["id"] == pick["id"], str(status))

        status, cells = request(f"/api/v1/backtest/picks?symbol={SYMBOL}&timeframe_seconds={TF}&censor_gap_seconds=60")
        check("saved cells reported for signature", status == 200 and len(cells) == 1 and cells[0]["strategy_key"] == "smoke-lab-pick", str(status))

        status, runs = request("/api/v1/backtest/sweeps")
        check("sweep surface history answers", status == 200 and len(runs) == 1 and runs[0]["cells"], str(status))

        # --- 2. Evidence export ----------------------------------------
        status, bundle = request(f"/api/v1/strategies/{strategy_id}/evidence")
        check("evidence bundle answers", status == 200 and bundle["origin"] == "backtest_lab", str(status))
        check("bundle carries provenance", len(bundle["lab_provenance"]) == 1 and bundle["lab_provenance"][0]["sweep_run_id"] == sweep["sweep_run_id"])

        status, csv_bytes = request(f"/api/v1/strategies/{strategy_id}/evidence/export.csv", raw=True)
        csv_text = csv_bytes.decode()
        check("csv export answers", status == 200 and "# Identity" in csv_text and "backtest_lab" in csv_text, str(status))

        req = urllib.request.Request(f"{BASE}/api/v1/strategies/{strategy_id}/evidence/export.csv", headers={"X-TradingOS-Token": ADMIN})
        with urllib.request.urlopen(req, timeout=15) as response:
            disposition = response.headers.get("Content-Disposition", "")
            check("csv disposition + filename", "attachment" in disposition and "evidence-smoke-lab-pick-v1.csv" in disposition, disposition)

        status, pdf_bytes = request(f"/api/v1/strategies/{strategy_id}/evidence/export.pdf", raw=True)
        check("pdf export is a valid pdf", status == 200 and pdf_bytes.startswith(b"%PDF-") and b"%%EOF" in pdf_bytes, f"{len(pdf_bytes)} bytes")

        # --- 3. Guard alerting ------------------------------------------
        # The world is PAUSED with no broker connected: a tick must trip a guard.
        status, run = request("/api/v1/loop/run", "POST")
        check("manual tick skips in paused world", status == 200 and run.get("summary", {}).get("skipped") is True, str(status))

        status, alerts = request("/api/v1/alerts")
        check("guard trip raised an alert", status == 200 and alerts["unacknowledged"] >= 1 and any(a["code"] == "LOOP_GUARD_TRIPPED" for a in alerts["alerts"]), str(alerts.get("unacknowledged")))

        first = next(a for a in alerts["alerts"] if a["code"] == "LOOP_GUARD_TRIPPED")
        status, run2 = request("/api/v1/loop/run", "POST")
        status, alerts2 = request("/api/v1/alerts")
        deduped = [a for a in alerts2["alerts"] if a["code"] == "LOOP_GUARD_TRIPPED" and not a["acknowledged"]]
        check("second tick dedupes into occurrences", len(deduped) >= 1 and any(a["occurrences"] >= 2 for a in deduped), str([(a['id'], a['occurrences']) for a in deduped]))

        status, ack = request(f"/api/v1/alerts/{first['id']}/ack", "POST")
        check("operator ack works", status == 200 and ack["acknowledged"] == [first["id"]], str(status))

        status, unread = request("/api/v1/alerts/unread-count")
        check("unread count after ack", status == 200 and unread["unacknowledged"] == alerts2["unacknowledged"] - 1, str(unread))

        status, denied = request("/api/v1/alerts/ack-all", "POST", token=None)
        check("ack endpoints are admin-gated", status == 401, str(status))

    finally:
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    failures = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failures)}/{len(results)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
