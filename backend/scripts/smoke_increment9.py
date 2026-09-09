"""Task 9 live smoke: alert rules, webhook retry policy, dedicated alerts page, AI status.

Boots a real uvicorn server against a throwaway database with a real local
webhook receiver, seeds candles in process (sharing TRADINGOS_DATABASE_URL
with the subprocess), and walks the whole alerting chain end to end.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from math import sin, tau

BASE = "http://127.0.0.1:8139"
ADMIN = "smoke-admin-token"
SYMBOL = "SMOKESYM"
TF = 60
SIGNING_SECRET = "smoke-signing-secret"

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}")


def request(path: str, method: str = "GET", token: str | None = ADMIN, payload: dict | None = None):
    body = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-TradingOS-Token"] = token
    if payload is not None:
        body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


# --------------------------------------------------------------- receiver
received: list[dict] = []
_recv_lock = threading.Lock()


class _Receiver(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        with _recv_lock:
            received.append({
                "path": self.path,
                "body": raw,
                "signature": self.headers.get("X-TradingOS-Signature", ""),
                "event": self.headers.get("X-TradingOS-Event", ""),
                "content_type": self.headers.get("Content-Type", ""),
            })
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args) -> None:
        pass


def wait_for_receipts(count: int, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _recv_lock:
            if len(received) >= count:
                return True
        time.sleep(0.2)
    return False


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="tradingos-smoke9-")
    fernet_key = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
    receiver = HTTPServer(("127.0.0.1", 0), _Receiver)
    recv_port = receiver.server_address[1]
    threading.Thread(target=receiver.serve_forever, daemon=True).start()

    # A port with nothing listening, for the refusal checks.
    dead_socket = socket.socket()
    dead_socket.bind(("127.0.0.1", 0))
    dead_port = dead_socket.getsockname()[1]
    dead_socket.close()

    env = dict(os.environ)
    env.update({
        "TRADINGOS_DATABASE_URL": f"sqlite:///{tmpdir}/smoke.db",
        "TRADINGOS_LOCAL_ADMIN_TOKEN": ADMIN,
        "TRADINGOS_CREDENTIAL_ENCRYPTION_KEY": fernet_key,
        "TRADINGOS_LOOP_ENABLED": "false",
        "TRADINGOS_ALERT_COOLDOWN_SECONDS": "5",
        "TRADINGOS_ALERT_WEBHOOK_URL": f"http://127.0.0.1:{recv_port}/hook",
        "TRADINGOS_WEBHOOK_SIGNING_SECRET": SIGNING_SECRET,
        "TRADINGOS_WEBHOOK_MAX_ATTEMPTS": "2",
        "TRADINGOS_WEBHOOK_BACKOFF_BASE_SECONDS": "1",
        "TRADINGOS_WEBHOOK_BACKOFF_MAX_SECONDS": "30",
        "TRADINGOS_WEBHOOK_TIMEOUT_SECONDS": "5",
        "TRADINGOS_AI_ENABLED": "true",
        "TRADINGOS_AI_API_KEY": "sk-smoke-deepseek-key-123",
        "TRADINGOS_AI_BASE_URL": "https://api.deepseek.com",
        "TRADINGOS_AI_MODEL": "deepseek-chat",
    })
    # Seeder lesson (learned twice): the in-process seeder shares the server env.
    os.environ.update(env)

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
        [".venv/bin/uvicorn", "app.main:app", "--port", "8139", "--log-level", "warning"],
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

        # --- 1. Rules seeded + policy reflects env ----------------------
        status, rules = request("/api/v1/alerts/rules")
        codes = {rule["code"] for rule in rules.get("rules", [])}
        check("rules seeded from manifest", status == 200 and codes == {"LOOP_GUARD_TRIPPED", "LOOP_TICK_FAILED", "LOOP_SUBMIT_ERROR"}, str(codes))

        status, policy = request("/api/v1/alerts/webhook/policy")
        check("policy reflects environment", status == 200 and policy["max_attempts"] == 2 and policy["signing_enabled"] is True and policy["target_configured"] is True and policy["backoff_base_seconds"] == 1, str(policy))

        # --- 2. Guard trip -> signed delivery -> receiver ---------------
        status, tick = request("/api/v1/loop/run", "POST")
        check("paused-world tick trips guard", status == 200 and tick.get("summary", {}).get("skipped") is True, str(status))

        check("receiver got the signed alert POST", wait_for_receipts(1), f"{len(received)} receipts")
        first = received[0] if received else {}
        expected_sig = "sha256=" + hmac.new(SIGNING_SECRET.encode(), first.get("body", b""), hashlib.sha256).hexdigest()
        check("delivery signature verifies", first.get("signature") == expected_sig, f"{first.get('signature')} vs {expected_sig}")
        body = json.loads(first.get("body", b"{}") or b"{}")
        check("alert payload carries event and code", body.get("event") == "alert" and body.get("code") == "LOOP_GUARD_TRIPPED", str(body)[:120])

        status, deliveries = request("/api/v1/alerts/deliveries")
        alert_deliveries = [d for d in deliveries.get("deliveries", []) if d["event"] == "alert"]
        check("delivery ledger marks DELIVERED", status == 200 and alert_deliveries and alert_deliveries[0]["status"] == "DELIVERED" and alert_deliveries[0]["attempts"] == 1 and alert_deliveries[0]["last_http_status"] == 200, str(alert_deliveries[:1]))

        status, alerts = request("/api/v1/alerts")
        check("guard alert raised and unread", status == 200 and alerts["unacknowledged"] >= 1 and any(a["code"] == "LOOP_GUARD_TRIPPED" for a in alerts["alerts"]), str(alerts["unacknowledged"]))

        # --- 3. Rule severity override on a fresh alert -----------------
        status, updated = request("/api/v1/alerts/rules/LOOP_GUARD_TRIPPED", "PUT", payload={"severity": "ERROR"})
        check("rule severity override saved", status == 200 and updated["severity"] == "ERROR", str(updated))

        time.sleep(6)  # cooldown is 5s in this smoke
        status, tick2 = request("/api/v1/loop/run", "POST")
        check("second tick after cooldown", status == 200, str(status))
        status, alerts2 = request("/api/v1/alerts")
        guard_rows = [a for a in alerts2["alerts"] if a["code"] == "LOOP_GUARD_TRIPPED"]
        check("fresh alert carries rule severity", len(guard_rows) >= 2 and guard_rows[0]["severity"] == "ERROR" and guard_rows[0]["occurrences"] == 1, str([(a["severity"], a["occurrences"]) for a in guard_rows]))

        # --- 4. Rule disable silences the code --------------------------
        status, _ = request("/api/v1/alerts/rules/LOOP_GUARD_TRIPPED", "PUT", payload={"enabled": False})
        time.sleep(6)
        status, tick3 = request("/api/v1/loop/run", "POST")
        summary3 = tick3.get("summary", {})
        check("silenced tick still reports skip", status == 200 and summary3.get("skipped") is True and summary3.get("guard_alert") is None, str(summary3))
        status, alerts3 = request("/api/v1/alerts")
        check("silenced code raises nothing", len([a for a in alerts3["alerts"] if a["code"] == "LOOP_GUARD_TRIPPED"]) == len(guard_rows), str(len(alerts3["alerts"])))

        status, _ = request("/api/v1/alerts/rules/LOOP_GUARD_TRIPPED", "PUT", payload={"enabled": True, "notify_webhook": False})
        time.sleep(6)
        with _recv_lock:
            before = len(received)
        status, _ = request("/api/v1/loop/run", "POST")
        time.sleep(2)
        status, alerts4 = request("/api/v1/alerts")
        rows4 = [a for a in alerts4["alerts"] if a["code"] == "LOOP_GUARD_TRIPPED"]
        check("notify_webhook=false raises alert without fanout", len(rows4) == len(guard_rows) + 1, f"alerts {len(rows4)}")
        with _recv_lock:
            check("no webhook fanout while rule says quiet", len(received) == before, f"{len(received)} vs {before}")

        # --- 5. TEST webhook + retry contracts --------------------------
        with _recv_lock:
            before_test = len(received)
        status, test = request("/api/v1/alerts/webhook/test", "POST")
        check("test webhook queued", status == 200 and test.get("status") == "PENDING", str(test))
        check("receiver got the TEST event", wait_for_receipts(before_test + 1), f"{len(received)} receipts")
        test_receipt = received[-1] if received else {}
        check("test event tagged", test_receipt.get("event") == "test", test_receipt.get("event", ""))

        status, deliveries2 = request("/api/v1/alerts/deliveries")
        delivered_row = next((d for d in deliveries2["deliveries"] if d["status"] == "DELIVERED"), None)
        check("retry of delivered row is 409", delivered_row is not None and request(f"/api/v1/alerts/deliveries/{delivered_row['id']}/retry", "POST")[0] == 409, str(delivered_row and delivered_row["id"]))
        check("unknown retry is 404", request("/api/v1/alerts/deliveries/999999/retry", "POST")[0] == 404)

        status, denied = request("/api/v1/alerts/webhook/test", "POST", token=None)
        check("test endpoint is admin-gated", status == 401, str(status))

        # --- 6. AI status ------------------------------------------------
        status, ai = request("/api/v1/ai/status")
        check("ai status reports ready", status == 200 and ai.get("ready") is True and ai.get("api_key_configured") is True and ai.get("model") == "deepseek-chat", str(ai))
        check("ai status never leaks the key", "sk-smoke-deepseek-key-123" not in json.dumps(ai), "")
        check("ai budget fields present", all(k in ai.get("budget", {}) for k in ("tokens_used_today", "token_budget", "runs_today", "run_limit")), str(ai.get("budget")))

        status, denied_ai = request("/api/v1/ai/status", token=None)
        check("ai status is admin-gated", status == 401, str(status))

    finally:
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        receiver.shutdown()

    failures = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failures)}/{len(results)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
