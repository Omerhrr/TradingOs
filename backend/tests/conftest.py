"""Isolated runtime for the TradingOS test suite.

Every test run gets a throwaway database, a configured admin token, and a
fresh Fernet key, regardless of any developer ``.env`` file or leftover local
``data/tradingos.db``. Environment is pinned here BEFORE any ``app`` module is
imported: pydantic-settings gives os.environ higher precedence than .env
files, so these values deterministically win.
"""

from __future__ import annotations

import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="tradingos-tests-")

os.environ["TRADINGOS_DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"
os.environ["TRADINGOS_LOCAL_ADMIN_TOKEN"] = "test-local-admin-token"
os.environ["TRADINGOS_AUTO_RECONCILE_ENABLED"] = "false"
os.environ["TRADINGOS_PRACTICE_EXECUTION_ENABLED"] = "false"
os.environ["TRADINGOS_REAL_EXECUTION_ENABLED"] = "false"

from cryptography.fernet import Fernet  # noqa: E402

os.environ["TRADINGOS_CREDENTIAL_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
