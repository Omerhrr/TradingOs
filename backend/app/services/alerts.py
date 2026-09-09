"""Operational alerting for loop guards and control-plane failures.

A guard trip is not an exception — it is the system working as designed — but
an operator still has to hear about it. Alerts are:

* **persisted** as ``Alert`` rows with an acknowledgement workflow,
* **rule-governed** — an :class:`AlertRule` per code decides whether the code
  raises at all, the severity it pages at, its dedupe cooldown, and whether it
  fans out to the external webhook (missing rules fall back to the built-in
  defaults, so seeding is a convenience, never a correctness dependency),
* **deduplicated** within a cooldown window: while a condition holds, the same
  code refreshes the existing unacknowledged alert (``occurrences`` grows)
  instead of flooding the ledger on every loop tick,
* **self-resolving** for guards: a later tick that passes its guard auto-
  acknowledges the standing guard alert so stale pages disappear,
* **fanned out** over the event bus (``alert.*`` events reach every WebSocket
  subscriber) and, when a rule allows it and a target is configured, queued as
  a persisted webhook delivery with a bounded retry policy.

Alerting is fire-and-forget by contract: a webhook outage, a dead loop, or a
full queue must never break a trading operation. The audit ledger remains the
system of record; alerts are the operable view on top of it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Alert, AlertRule, AuditEvent
from app.services import webhooks
from app.services.events import publish_event

_logger = logging.getLogger(__name__)

# Codes raised by the loop engine and its guards.
GUARD_TRIPPED = "LOOP_GUARD_TRIPPED"
TICK_FAILED = "LOOP_TICK_FAILED"
SUBMIT_ERROR = "LOOP_SUBMIT_ERROR"

# The rule table is seeded from this manifest: code, natural severity, and the
# operator-facing description shown on the alert center page.
RULE_MANIFEST: tuple[dict[str, str], ...] = (
    {"code": GUARD_TRIPPED, "severity": "WARNING", "description": "A loop tick was skipped by a fail-closed guard (paused, disconnected, candle-starved, or risk-capped)."},
    {"code": TICK_FAILED, "severity": "ERROR", "description": "A loop tick raised unexpectedly; the run was persisted as FAILED and the error surfaced."},
    {"code": SUBMIT_ERROR, "severity": "ERROR", "description": "Submitting a loop-originated order intent to the practice broker failed."},
)

VALID_SEVERITIES = ("INFO", "WARNING", "ERROR")


def ensure_alert_rules(session: Session) -> None:
    """Seed missing alert rules from the manifest. Idempotent; never updates existing rows."""
    existing = {row.code for row in session.scalars(select(AlertRule)).all()}
    for entry in RULE_MANIFEST:
        if entry["code"] in existing:
            continue
        session.add(AlertRule(
            code=entry["code"],
            enabled=True,
            severity=entry["severity"],
            cooldown_seconds=None,
            notify_webhook=True,
            description=entry["description"],
        ))
    session.commit()


def rule_for(session: Session, code: str) -> AlertRule | None:
    return session.scalar(select(AlertRule).where(AlertRule.code == code).limit(1))


def raise_alert(
    session: Session,
    settings: Settings,
    *,
    code: str,
    severity: str,
    message: str,
    payload: dict | None = None,
) -> Alert | None:
    """Raise (or refresh) an alert. Safe to call from any thread or request.

    Rule resolution order (a missing rule row means built-in defaults):

    * ``rule.enabled is False`` — the operator silenced this code: nothing is
      persisted, fanned out, or audited; ``None`` is returned and callers must
      handle that,
    * severity — the rule's severity wins over the caller's (operators may
      escalate or demote a code without touching trading code),
    * cooldown — the rule's window wins, else ``settings.alert_cooldown_seconds``.

    Dedupe rule: an UNACKNOWLEDGED alert with the same code whose
    ``last_seen_at`` is inside the cooldown window is refreshed in place —
    ``occurrences`` grows, ``last_seen_at``/message/payload are updated. Once
    acknowledged, the same condition raises a fresh row.
    """
    rule = rule_for(session, code)
    if rule is not None and not rule.enabled:
        return None
    effective_severity = rule.severity if rule is not None and rule.severity in VALID_SEVERITIES else severity
    cooldown_seconds = rule.cooldown_seconds if rule is not None and rule.cooldown_seconds is not None else settings.alert_cooldown_seconds

    now = datetime.now(UTC)
    cooldown_start = now - timedelta(seconds=cooldown_seconds)
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
        alert = Alert(code=code, severity=effective_severity, message=message, payload=payload or {}, last_seen_at=now)
        session.add(alert)
    session.add(AuditEvent(
        event_type="ALERT_RAISED",
        severity=effective_severity if effective_severity in {"WARNING", "ERROR"} else "WARNING",
        message=f"Operational alert {code}: {message}",
        payload={"code": code, "occurrences": alert.occurrences, **({"deduplicated": True} if existing is not None else {})},
    ))
    # The delivery rides the same transaction as the alert: either both are
    # durable or neither is. Flush first so a brand-new alert has its id before
    # the delivery row references it. The dispatcher is woken only after commit.
    session.flush()
    delivery = None
    if rule is None or rule.notify_webhook:
        delivery = webhooks.enqueue_delivery(session, settings, alert=alert, event="alert", code=code)
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
    if delivery is not None:
        webhooks.wake_dispatcher()
    return alert


def update_alert_rule(
    session: Session,
    settings: Settings,
    code: str,
    *,
    enabled: bool | None = None,
    severity: str | None = None,
    cooldown_seconds: int | None = None,
    notify_webhook: bool | None = None,
) -> AlertRule:
    """Partial update of one alert rule; ``None`` fields are left untouched."""
    rule = rule_for(session, code)
    if rule is None:
        raise LookupError(f"No alert rule for code {code}.")
    if severity is not None:
        if severity not in VALID_SEVERITIES:
            raise ValueError("Severity must be one of INFO, WARNING, ERROR.")
        rule.severity = severity
    if enabled is not None:
        rule.enabled = enabled
    if cooldown_seconds is not None:
        if not 5 <= cooldown_seconds <= 86_400:
            raise ValueError("Cooldown must be between 5 and 86400 seconds.")
        rule.cooldown_seconds = cooldown_seconds
    if notify_webhook is not None:
        rule.notify_webhook = notify_webhook
    rule.updated_at = datetime.now(UTC)
    session.add(AuditEvent(
        event_type="ALERT_RULE_UPDATED",
        severity="INFO",
        message=f"Alert rule {code} was updated by the operator.",
        payload={
            "code": code,
            "enabled": rule.enabled,
            "severity": rule.severity,
            "cooldown_seconds": rule.cooldown_seconds,
            "notify_webhook": rule.notify_webhook,
        },
    ))
    session.commit()
    session.refresh(rule)
    return rule


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


def serialize_rule(rule: AlertRule) -> dict:
    return {
        "id": rule.id,
        "code": rule.code,
        "enabled": rule.enabled,
        "severity": rule.severity,
        "cooldown_seconds": rule.cooldown_seconds,
        "notify_webhook": rule.notify_webhook,
        "description": rule.description,
        "updated_at": rule.updated_at,
    }
