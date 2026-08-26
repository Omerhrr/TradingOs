"""Practice execution must remain blocked unless the explicit local configuration switch is enabled."""

import pytest

from app.config import Settings
from app.services.execution import ExecutionService
from app.services.broker import BrokerError


class FakeBroker:
    pass


def test_practice_submission_is_blocked_by_default() -> None:
    service = ExecutionService(FakeBroker(), Settings(practice_execution_enabled=False))  # type: ignore[arg-type]
    with pytest.raises(BrokerError, match="disabled"):
        service.submit_approved_intent(None, 1)  # type: ignore[arg-type]
