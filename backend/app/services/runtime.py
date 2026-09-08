"""Opt-in local background runtime for a single authenticated PRACTICE broker session."""

from __future__ import annotations

import threading

from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import AccountConfig, AuditEvent, EncryptedBrokerCredential, SystemState
from app.services.credentials import BrokerCredentials, CredentialVault
from app.services.worker import BrokerWorker


class LocalRuntime:
    """Maintains one worker thread; any unexpected broker error halts new exposure."""

    def __init__(self, settings: Settings, session_factory: sessionmaker, worker: BrokerWorker, loop=None) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.worker = worker
        self.loop = loop
        self._stop = threading.Event()
        self._halted = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.settings.auto_reconcile_enabled and self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="tradingos-practice-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        self.worker.disconnect()

    def _halt(self, error: Exception) -> None:
        # Latch the halt: the documented fail-closed contract is that the worker
        # moves to HALTED and does not resume on its own. Without this latch the
        # loop below would simply reconnect on the next tick.
        self._halted.set()
        with self.session_factory() as session:
            account = session.query(AccountConfig).first()
            if account:
                account.system_state = SystemState.HALTED.value
            session.add(AuditEvent(event_type="LOCAL_RUNTIME_HALTED", severity="ERROR", message="Autonomous practice worker halted after a broker or reconciliation error.", payload={"error_type": type(error).__name__}))
            session.commit()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._halted.is_set():
                # Stay down until a human restarts the service. Fail closed.
                self._stop.wait(self.settings.broker_sync_interval_seconds)
                continue
            try:
                with self.session_factory() as session:
                    if self.worker.adapter.health().state != "CONNECTED":
                        credential = session.query(EncryptedBrokerCredential).first()
                        if credential is None:
                            self._stop.wait(self.settings.broker_sync_interval_seconds)
                            continue
                        vault = CredentialVault(self.settings.credential_encryption_key)
                        self.worker.connect_practice(session, BrokerCredentials(email=vault.decrypt(credential.email_ciphertext), password=vault.decrypt(credential.password_ciphertext)))
                    else:
                        if self.loop is not None and self.settings.loop_enabled:
                            # The loop reconciles internally before signalling,
                            # so one tick is the full observe -> decide -> act pass.
                            self.loop.tick(session)
                        else:
                            self.worker.reconcile(session)
            except Exception as exc:
                self._halt(exc)
                self.worker.disconnect()
            self._stop.wait(self.settings.broker_sync_interval_seconds)
