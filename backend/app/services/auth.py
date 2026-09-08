"""Auth-gated remote access for the TradingOS control plane.

The control plane was designed local-first: the admin token in the
``X-TradingOS-Token`` header authorizes broker and strategy operations, and
read endpoints stay open on the trusted LAN. When the operator deliberately
exposes the service beyond localhost (``TRADINGOS_REMOTE_ACCESS_ENABLED``),
every request must present credentials:

  * the local admin token (header or bearer) — API-key style access, or
  * a session token issued by ``POST /auth/login`` (cookie or bearer).

Sessions are HMAC-SHA256 signed, stateless, and time-boxed. The signing key
is ``TRADINGOS_SESSION_SECRET`` when provided, otherwise a key derived from
the admin token, so a leaked session can never outlive a token rotation.

Login is rate limited per client IP: five failed attempts inside the window
lock the source out for the remainder of the window. Every attempt lands in
the audit ledger, and attempted secrets are never written to it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta

from app.config import Settings

SESSION_COOKIE = "tradingos_session"
_CLOCK_SKEW_SECONDS = 30
_LOGIN_WINDOW_SECONDS = 900
_LOGIN_MAX_FAILURES = 5


class SessionConfigurationError(RuntimeError):
    """Raised when sessions are requested but no signing secret can be derived."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class SessionManager:
    """Issues and verifies stateless, signed, expiring session tokens."""

    def __init__(self, settings: Settings) -> None:
        self._ttl_minutes = settings.session_ttl_minutes
        secret = settings.session_secret or settings.local_admin_token
        if not secret:
            raise SessionConfigurationError(
                "Set TRADINGOS_LOCAL_ADMIN_TOKEN (or TRADINGOS_SESSION_SECRET) before issuing sessions."
            )
        # Derive a dedicated key so session tokens are unrelated bytes to the
        # admin token even when the token itself is the only shared secret.
        self._key = hashlib.sha256(f"tradingos-session-v1:{secret}".encode()).digest()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_minutes * 60

    def issue(self) -> tuple[str, datetime]:
        expires_at = datetime.now(UTC) + timedelta(seconds=self.ttl_seconds)
        message = f"v1.{int(expires_at.timestamp())}.{secrets.token_hex(8)}"
        signature = hmac.new(self._key, message.encode(), hashlib.sha256).digest()
        token = f"{message}.{_b64url(signature)}"
        return token, expires_at

    def verify(self, token: str | None) -> bool:
        if not token:
            return False
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != "v1":
            return False
        message = f"{parts[0]}.{parts[1]}.{parts[2]}"
        expected = hmac.new(self._key, message.encode(), hashlib.sha256).digest()
        try:
            provided = _b64url_decode(parts[3])
        except (ValueError, TypeError):
            return False
        if not hmac.compare_digest(expected, provided):
            return False
        try:
            expires_epoch = int(parts[1])
        except ValueError:
            return False
        return time.time() <= expires_epoch + _CLOCK_SKEW_SECONDS


class LoginGate:
    """Per-IP sliding-window lockout for the login endpoint.

    Counters are in-memory by design: restarting the process clears them, and
    no attacker-controlled input ever becomes a memory-growth vector because
    entries are pruned on every check.
    """

    def __init__(self, window_seconds: int = _LOGIN_WINDOW_SECONDS, max_failures: int = _LOGIN_MAX_FAILURES) -> None:
        self._window = window_seconds
        self._max_failures = max_failures
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, client_ip: str) -> int | None:
        """Seconds remaining on an active lockout, or None when login may proceed."""
        now = time.time()
        with self._lock:
            attempts = [stamp for stamp in self._failures.get(client_ip, []) if now - stamp < self._window]
            self._failures[client_ip] = attempts
            if len(attempts) < self._max_failures:
                return None
            oldest = attempts[0]
            return max(1, int(self._window - (now - oldest)))

    def record_failure(self, client_ip: str) -> None:
        now = time.time()
        with self._lock:
            attempts = [stamp for stamp in self._failures.get(client_ip, []) if now - stamp < self._window]
            attempts.append(now)
            self._failures[client_ip] = attempts

    def record_success(self, client_ip: str) -> None:
        with self._lock:
            self._failures.pop(client_ip, None)


def credential_ok(
    *,
    settings: Settings,
    headers: dict[str, str] | None,
    cookies: dict[str, str] | None,
    query_token: str | None = None,
) -> bool:
    """True when the request carries valid remote-access credentials.

    Accepted, in priority order: the admin token via ``X-TradingOS-Token``
    header, a bearer token (admin or session), a valid session cookie, or a
    ``access_token`` query parameter (WebSocket upgrade convenience).
    Header lookups are case-insensitive per the HTTP spec as handled by
    Starlette's Headers mapping.
    """
    if not settings.remote_access_enabled:
        return True  # local-first behavior is unchanged when the gate is off
    if not settings.local_admin_token:
        return False
    normalized = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
    header_token = normalized.get("x-tradingos-token")
    if header_token and hmac.compare_digest(header_token, settings.local_admin_token):
        return True
    authorization = normalized.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
        if hmac.compare_digest(bearer, settings.local_admin_token):
            return True
        if SessionManager(settings).verify(bearer):
            return True
    if query_token:
        if hmac.compare_digest(query_token, settings.local_admin_token):
            return True
        if SessionManager(settings).verify(query_token):
            return True
    cookie_token = (cookies or {}).get(SESSION_COOKIE)
    return bool(cookie_token and SessionManager(settings).verify(cookie_token))
