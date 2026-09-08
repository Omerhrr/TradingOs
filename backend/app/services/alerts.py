"""Operational alerting for loop guards and control-plane failures.

A guard trip is not an exception — it is the system working as designed — but
an operator still has to hear about it. Alerts are:

* **persisted** as ``Alert`` rows with an acknowledgement workflow,
* **deduplicated** within a cooldown window: while a condition holds, the same
  code refreshes the existing unacknowledged alert (``occurrences`` grows)
  instead of flooding the ledger on every loop tick,
* **self-resolving** for guards: a later tick that passes its guard auto-
  acknowledges the standing guard alert so stale pages disappear,
* **fanned out** over the existing event bus (``alert.*`` events reach every
  WebSocket subscriber) and optionally POSTed to an external webhook.

Alerting is fire-and-forget by contract: a webhook outage, a dead loop, or a
full queue must never break a trading operation. The audit ledger remains the
system of record; alerts are the operable view on top of it.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Alert, AuditEvent
from app.services.events import publish_event

_logger = logging.getLogger(__name__)

# Codes raised by the loop engine and its guards.
GUARD_TRIPPED = "LOOP_GUARD_TRIPPED"
TICK_FAILED = "LOOP_TICK_FAILED"
SUBMIT_ERROR = "LOOP_SUBMIT_ERROR"


def raise_alert(
    session: Session,
    settings: Settings,
    *,
    code: str,
    severity: str,
    message: str,
    payload: dict | None = None,
) -> Alert:
    """Raise (or refresh) an alert. Safe to call from any thread or request.

    Dedupe rule: an UNACKNOWLEDGED alert with the same code whose
    ``last_seen_at`` is inside the cooldown window is refreshed in place —
    ``occurrences`` grows, ``last_seen_at``/message/payload are updated. Once
    acknowledged, the same condition raises a fresh row.
    """
    now = datetime.now(UTC)
    cooldown_start = now - timedelta(seconds=settings.alert_cooldown_seconds)
    existing = session.scalar(
        select(Alert)
        .where(Alert.code == code)
        .where(Alert.acknowledged_at.is_(None))
        .where(Alert.last_seen_at >= cooldown_start)
        .order_by(Alert.id.desc())
        .limit(1)
    )
    if existing is not None:
        existing.occurrences += 1
        existing.last_seen_at = now
        existing.message = message
        existing.payload = payload or {}
        alert = existing
    else:
        alert = Alert(code=code, severity=severity, message=message, payload=payload or {}, last_seen_at=now)
        session.add(alert)
    session.add(AuditEvent(
        event_type="ALERT_RAISED",
        severity=severity if severity in {"WARNING", "ERROR"} else "WARNING",
        message=f"Operational alert {code}: {message}",
        payload={"code": code, "occurrences": alert.occurrences, **({"deduplicated": True} if existing is not None else {})},
    ))
    session.commit()
    session.refresh(alert)
    publish_event("alert.raised", {
        "alert_id": alert.id,
        "code": alert.code,
        "severity": alert.severity,
        "message": alert.message,
        "occurrences": alert.occurrences,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
    })
    _notify_webhook(settings, alert)
    return alert


def resolve_guard_alerts(session: Session, settings: Settings) -> int:
    """Auto-acknowledge standing guard alerts after a tick passed its guards.

    The condition the operator was warned about is gone; leaving the page up
    would train them to ignore alerts. Returns how many alerts resolved.
    """
    standing = list(session.scalars(
        select(Alert).where(Alert.code == GUARD_TRIPPED).where(Alert.acknowledged_at.is_(None))
    ))
    now = datetime.now(UTC)
    for alert in standing:
        alert.acknowledged_at = now
    if standing:
        session.add(AuditEvent(
            event_type="ALERT_RESOLVED",
            severity="INFO",
            message="A loop tick passed its guards; the standing guard alert auto-resolved.",
            payload={"alert_ids": [alert.id for alert in standing], "auto": True},
        ))
        session.commit()
        publish_event("alert.acknowledged", {"alert_ids": [alert.id for alert in standing], "auto": True, "code": GUARD_TRIPPED})
    return len(standing)


def acknowledge_alert(session: Session, settings: Settings, alert_id: int) -> Alert:
    alert = session.get(Alert, alert_id)
    if alert is None:
        raise ValueError(f"Alert {alert_id} does not exist.")
    if alert.acknowledged_at is None:
        alert.acknowledged_at = datetime.now(UTC)
        session.add(AuditEvent(
            event_type="ALERT_ACKNOWLEDGED",
            severity="INFO",
            message=f"Alert {alert.code} was acknowledged by the operator.",
            payload={"alert_id": alert.id, "code": alert.code},
        ))
        session.commit()
        session.refresh(alert)
        publish_event("alert.acknowledged", {"alert_ids": [alert.id], "auto": False, "code": alert.code})
    return alert


def acknowledge_all(session: Session, settings: Settings) -> list[int]:
    """Acknowledge every unacknowledged alert; returns the ids that were cleared."""
    standing = list(session.scalars(select(Alert).where(Alert.acknowledged_at.is_(None))))
    now = datetime.now(UTC)
    for alert in standing:
        alert.acknowledged_at = now
    if standing:
        session.add(AuditEvent(
            event_type="ALERT_ACKNOWLEDGED",
            severity="INFO",
            message=f"{len(standing)} alert(s) were acknowledged by the operator in one action.",
            payload={"alert_ids": [alert.id for alert in standing], "count": len(standing)},
        ))
        session.commit()
        publish_event("alert.acknowledged", {"alert_ids": [alert.id for alert in standing], "auto": False})
    return [alert.id for alert in standing]


def serialize_alert(alert: Alert) -> dict:
    return {
        "id": alert.id,
        "code": alert.code,
        "severity": alert.severity,
        "message": alert.message,
        "payload": alert.payload or {},
        "occurrences": alert.occurrences,
        "acknowledged": alert.acknowledged_at is not None,
        "acknowledged_at": alert.acknowledged_at,
        "created_at": alert.created_at,
        "last_seen_at": alert.last_seen_at,
    }


def _notify_webhook(settings: Settings, alert: Alert) -> None:
    """Fire-and-forget outbound notification. Never raises, never blocks ops."""
    if not settings.alert_webhook_url:
        return

    def _post() -> None:
        try:
            body = json.dumps({
                "code": alert.code,
                "severity": alert.severity,
                "message": alert.message,
                "occurrences": alert.occurrences,
                "alert_id": alert.id,
            }).encode()
            request = urllib.request.Request(
                settings.alert_webhook_url,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "TradingOS-Alerts/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5):
                pass
        except Exception:  # noqa: BLE001 — a webhook outage is never an ops problem
            _logger.debug("Alert webhook delivery failed for %s", alert.code, exc_info=True)

    threading.Thread(target=_post, daemon=True, name="tradingos-alert-webhook").start()
