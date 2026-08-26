"""Single-writer local worker facade for connection and reconciliation operations."""

from __future__ import annotations

import threading

from sqlalchemy.orm import Session

from app.services.broker import BrokerAdapter
from app.services.credentials import BrokerCredentials
from app.services.reconciliation import Reconciler


class BrokerWorker:
    """Serializes all broker work in one process, preventing competing account sessions."""

    def __init__(self, adapter: BrokerAdapter, candle_count: int) -> None:
        self.adapter = adapter
        self.reconciler = Reconciler(adapter, candle_count)
        self._lock = threading.RLock()

    def connect_practice(self, session: Session, credentials: BrokerCredentials):
        with self._lock:
            account = self.adapter.connect_practice(credentials)
            self.reconciler.run(session)
            return account

    def reconcile(self, session: Session):
        with self._lock:
            return self.reconciler.run(session)

    def disconnect(self) -> None:
        with self._lock:
            self.adapter.disconnect()
