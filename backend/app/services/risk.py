"""Deterministic risk checks independent of any LLM or broker connection."""

from __future__ import annotations

from dataclasses import dataclass

from app.models import AccountMode, SystemState


@dataclass(frozen=True)
class RiskDecision:
    accepted: bool
    reason: str
    approved_amount: float | None = None


def gate_entry(
    *,
    account_mode: str,
    system_state: str,
    broker_connected: bool,
    market_is_fresh: bool,
    open_positions: int,
    max_open_positions: int,
    requested_amount: float,
    max_trade_amount: float,
    daily_loss_fraction: float = 0.0,
    max_daily_loss_fraction: float = 0.02,
    drawdown_fraction: float = 0.0,
    max_drawdown_fraction: float = 0.05,
) -> RiskDecision:
    """Fail closed. This is the only acceptable default for uncertain operational state."""

    if account_mode != AccountMode.PRACTICE.value:
        return RiskDecision(False, "Only practice mode is eligible in the current scaffold.")
    if system_state != SystemState.ACTIVE.value:
        return RiskDecision(False, "System is not active for new exposure.")
    if not broker_connected:
        return RiskDecision(False, "Broker connection is unavailable.")
    if not market_is_fresh:
        return RiskDecision(False, "Market data is stale.")
    if open_positions >= max_open_positions:
        return RiskDecision(False, "Maximum concurrent exposure reached.")
    if daily_loss_fraction >= max_daily_loss_fraction:
        return RiskDecision(False, "Daily loss limit is reached.")
    if drawdown_fraction >= max_drawdown_fraction:
        return RiskDecision(False, "Maximum drawdown limit is reached.")
    if requested_amount <= 0:
        return RiskDecision(False, "Requested amount must be positive.")
    return RiskDecision(True, "Practice risk checks passed.", min(requested_amount, max_trade_amount))
