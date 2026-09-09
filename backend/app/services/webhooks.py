"""Persisted webhook deliveries with a bounded exponential-backoff retry worker.

The previous notification path was a single fire-and-forget POST: one transient
network hiccup and the operator silently never heard about a guard trip. Now
every notification is a persisted :class:`WebhookDelivery` row enqueued in the
same transaction as its alert, and a background dispatcher works the queue:

* **exponential backoff** — ``base * 2**(attempt-1)`` seconds, capped at the
  configured maximum,
* **bounded budget** — after ``max_attempts`` transport failures the row is
  marked EXHAUSTED (and audited) instead of retrying forever,
* **manual retry** — an operator can reopen an EXHAUSTED row with a fresh
  attempt budget,
* **authenticity** — when a signing secret is configured, each request carries
  ``X-TradingOS-Signature: sha256=<hmac_sha256(secret, body)>`` so a receiver
  can verify the caller,
* **never breaks trading ops** — the request path only ever inserts a row and
  sets an event; every transport error lands in the row, not in the caller.

The audit ledger remains the system of record; deliveries are the operable
view of "did the operator get told, and do we have proof we tried".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Alert, AuditEvent, WebhookDelivery
from app.services.events import publish_event

_logger = logging.getLogger(__name__)


def _http_post(url: str, body: bytes, headers: dict[str, str], timeout_seconds: int) -> int:
    """Perform the POST and return the HTTP status; raise on transport failure."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return int(response.status)


def sign_body(secret: str, body: bytes) -> str:
    """``sha256=<hex>`` signature header value for an outbound webhook body."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def enqueue_delivery(
    session: Session,
    settings: Settings,
    *,
    alert: Alert | None = None,
    event: str = "alert",
    code: str,
) -> WebhookDelivery | None:
    """Add a PENDING delivery row to ``session`` (no commit, no network).

    Called inside :func:`app.services.alerts.raise_alert` so the delivery is
    persisted atomically with the alert it announces. Returns ``None`` (and
    does nothing) when no webhook target is configured — alerting stays fully
    functional without one. The caller is responsible for waking the
    dispatcher after its own commit.
    """
    if not settings.alert_webhook_url:
        return None
    body = {
        "event": event,
        "code": code,
        "alert_id": alert.id if alert is not None else None,
        "severity": alert.severity if alert is not None else "INFO",
        "message": alert.message if alert is not None else f"TradingOS {event} notification for {code}.",
        "occurrences": alert.occurrences if alert is not None else 1,
        "queued_at": datetime.now(UTC).isoformat(),
    }
    delivery = WebhookDelivery(
        alert_id=alert.id if alert is not None else None,
        event=event,
        code=code,
        target_url=settings.alert_webhook_url,
        status="PENDING",
        attempts=0,
        max_attempts=settings.webhook_max_attempts,
        next_attempt_at=datetime.now(UTC),
        body=body,
    )
    session.add(delivery)
    return delivery


def backoff_seconds(settings: Settings, attempts: int) -> int:
    """Exponential backoff after the Nth failed attempt (1-based), capped."""
    raw = settings.webhook_backoff_base_seconds * (2 ** (attempts - 1))
    return min(raw, settings.webhook_backoff_max_seconds)


class WebhookDispatcher:
    """Background worker that delivers due webhook deliveries.

    One daemon thread per process. It sleeps on an event with a timeout so it
    wakes immediately when a delivery is enqueued and at least every
    ``poll_interval_seconds`` for rows whose backoff came due. Every session
    is opened per batch from the injected factory — the dispatcher never
    touches the request session or the runtime's session.
    """

    def __init__(self, session_factory: sessionmaker, settings: Settings, poll_interval_seconds: float = 5.0) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._poll_interval = poll_interval_seconds
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._batch_size = 20

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._wake_event.set()  # drain anything queued before startup
        self._thread = threading.Thread(target=self._run, daemon=True, name="tradingos-webhook-dispatcher")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)
        self._thread = None

    def wake(self) -> None:
        """Nudge the dispatcher from any thread; safe before start() and after stop()."""
        self._wake_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=self._poll_interval)
            self._wake_event.clear()
            if self._stop_event.is_set():
                return
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — a dispatcher crash must not kill the thread
                _logger.exception("Webhook dispatcher batch failed")

    # ----------------------------------------------------------------- delivery
    def run_once(self) -> int:
        """Deliver every due row once; returns how many rows were attempted."""
        attempted = 0
        while attempted < self._batch_size:
            with self._session_factory() as session:
                now = datetime.now(UTC)
                due = session.scalar(
                    select(WebhookDelivery)
                    .where(WebhookDelivery.status == "PENDING")
                    .where(WebhookDelivery.next_attempt_at.is_not(None))
                    .where(WebhookDelivery.next_attempt_at <= now)
                    .order_by(WebhookDelivery.id)
                    .limit(1)
                )
                if due is None:
                    return attempted
                self._attempt(session, due)
                attempted += 1
        return attempted

    def _attempt(self, session: Session, delivery: WebhookDelivery) -> None:
        delivery.attempts += 1
        delivery.updated_at = datetime.now(UTC)
        body = json.dumps(delivery.body or {}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "TradingOS-Alerts/1.1",
            "X-TradingOS-Event": delivery.event,
            "X-TradingOS-Delivery": str(delivery.id),
        }
        if self._settings.webhook_signing_secret:
            headers["X-TradingOS-Signature"] = sign_body(self._settings.webhook_signing_secret, body)
        try:
            status_code = _http_post(
                delivery.target_url,
                body,
                headers,
                self._settings.webhook_timeout_seconds,
            )
            delivery.status = "DELIVERED"
            delivery.delivered_at = datetime.now(UTC)
            delivery.last_http_status = status_code
            delivery.last_error = None
            delivery.next_attempt_at = None
            session.add(AuditEvent(
                event_type="WEBHOOK_DELIVERED",
                severity="INFO",
                message=f"Webhook delivery #{delivery.id} for {delivery.code} was accepted by the receiver.",
                payload={"delivery_id": delivery.id, "code": delivery.code, "event": delivery.event, "attempts": delivery.attempts, "http_status": status_code},
            ))
            session.commit()
            publish_event("webhook.delivered", {"delivery_id": delivery.id, "code": delivery.code, "event": delivery.event, "attempts": delivery.attempts})
        except Exception as exc:  # noqa: BLE001 — every failure lands in the row
            delivery.last_http_status = getattr(exc, "code", None) if isinstance(getattr(exc, "code", None), int) else None
            message = f"{type(exc).__name__}: {exc}"[:500]
            delivery.last_error = message
            if delivery.attempts >= delivery.max_attempts:
                delivery.status = "EXHAUSTED"
                delivery.next_attempt_at = None
                session.add(AuditEvent(
                    event_type="WEBHOOK_EXHAUSTED",
                    severity="WARNING",
                    message=f"Webhook delivery #{delivery.id} for {delivery.code} exhausted its retry budget; the receiver may not have heard about {delivery.code}.",
                    payload={"delivery_id": delivery.id, "code": delivery.code, "attempts": delivery.attempts, "error": message},
                ))
                session.commit()
                publish_event("webhook.exhausted", {"delivery_id": delivery.id, "code": delivery.code, "attempts": delivery.attempts})
            else:
                delay = backoff_seconds(self._settings, delivery.attempts)
                delivery.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                session.commit()
                _logger.debug("Webhook delivery #%s attempt %s failed; retrying in %ss", delivery.id, delivery.attempts, delay, exc_info=True)


def retry_delivery(session: Session, settings: Settings, delivery_id: int) -> WebhookDelivery:
    """Reopen an EXHAUSTED (or stuck PENDING) delivery with a fresh attempt budget."""
    delivery = session.get(WebhookDelivery, delivery_id)
    if delivery is None:
        raise LookupError(f"Webhook delivery {delivery_id} does not exist.")
    if delivery.status == "DELIVERED":
        raise ValueError(f"Webhook delivery {delivery_id} was already delivered.")
    delivery.status = "PENDING"
    delivery.attempts = 0  # manual retry = fresh budget, documented semantics
    delivery.next_attempt_at = datetime.now(UTC)
    delivery.updated_at = datetime.now(UTC)
    session.add(AuditEvent(
        event_type="WEBHOOK_RETRY_QUEUED",
        severity="INFO",
        message=f"Webhook delivery #{delivery.id} for {delivery.code} was queued for redelivery by the operator.",
        payload={"delivery_id": delivery.id, "code": delivery.code},
    ))
    session.commit()
    session.refresh(delivery)
    return delivery


# ---------------------------------------------------- process-wide registration
_dispatcher: WebhookDispatcher | None = None
_registration_lock = threading.Lock()


def register_dispatcher(dispatcher: WebhookDispatcher) -> None:
    """Bind the process-wide dispatcher (main.py lifespan). Wake stays a no-op until then."""
    global _dispatcher
    with _registration_lock:
        _dispatcher = dispatcher


def wake_dispatcher() -> None:
    """Safe-from-any-thread nudge; silently does nothing when unregistered."""
    dispatcher = _dispatcher
    if dispatcher is not None:
        try:
            dispatcher.wake()
        except Exception:  # noqa: BLE001 — notification plumbing never breaks ops
            _logger.debug("Waking the webhook dispatcher failed", exc_info=True)
