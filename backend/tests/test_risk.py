"""Risk gates must stay deterministic and fail-closed."""

from app.models import AccountMode, SystemState
from app.services.risk import gate_entry


def test_risk_gate_rejects_non_practice_mode() -> None:
    result = gate_entry(account_mode=AccountMode.REAL.value, system_state=SystemState.ACTIVE.value, broker_connected=True, market_is_fresh=True, open_positions=0, max_open_positions=1, requested_amount=1, max_trade_amount=5)
    assert result.accepted is False
    assert "practice" in result.reason.lower()


def test_risk_gate_rejects_stale_market_data() -> None:
    result = gate_entry(account_mode=AccountMode.PRACTICE.value, system_state=SystemState.ACTIVE.value, broker_connected=True, market_is_fresh=False, open_positions=0, max_open_positions=1, requested_amount=1, max_trade_amount=5)
    assert result.accepted is False
    assert "stale" in result.reason.lower()
