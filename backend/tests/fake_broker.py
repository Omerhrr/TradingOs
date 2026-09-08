"""Scriptable in-memory broker adapter for integration and failure-injection tests.

It mirrors the exact contract of IQAirBrokerAdapter so tests exercise the real
service layer (reconciler, execution service, runtime) without network access:

  connect_practice  -> AUTH_FAILED / MODE_MISMATCH / CONNECTED
  account()         -> raises BrokerError on mode drift (fail-closed parity)
  candles()         -> iqair-shaped payloads (from/to/open/max/min/close)
  positions()       -> scriptable position payloads for settlement flows
  submit_practice_option -> records submissions, optional failure injection
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.broker import BrokerAccount, BrokerAdapter, BrokerError, BrokerHealth
from app.services.credentials import BrokerCredentials


@dataclass
class FakeBrokerAdapter(BrokerAdapter):
    """Every failure point is a writable attribute a test can flip mid-flight."""

    balance: float = 1_000.0
    currency: str = "USD"
    # Mode the fake reports on connect and on every subsequent account() call.
    connect_mode: str = "PRACTICE"
    live_mode: str = "PRACTICE"
    # Failure injection switches.
    fail_connect: bool = False
    fail_candles_on: set[str] = field(default_factory=set)
    fail_submit: bool = False
    fail_account: bool = False
    # Recorded activity.
    submitted: list[dict[str, Any]] = field(default_factory=list)
    connect_calls: int = 0
    disconnect_calls: int = 0
    candles_by_symbol: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    positions_payload: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._health = BrokerHealth()
        self._connected = False

    # ------------------------------------------------------------- lifecycle
    def health(self) -> BrokerHealth:
        return self._health

    def connect_practice(self, credentials: BrokerCredentials) -> BrokerAccount:
        self.connect_calls += 1
        if self.fail_connect:
            self._health = BrokerHealth("AUTH_FAILED", "Broker authentication or websocket connection failed.")
            raise BrokerError("Practice connection failed: invalid credentials")
        if self.connect_mode != "PRACTICE":
            self._health = BrokerHealth("MODE_MISMATCH", "Broker did not confirm the PRACTICE balance.")
            raise BrokerError("The broker did not confirm PRACTICE balance mode; no operation was started.")
        self._connected = True
        self._health = BrokerHealth("CONNECTED", "Authenticated to the PRACTICE balance.")
        return self.account()

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False
        self._health = BrokerHealth("DISCONNECTED", "Broker session closed.")

    # ------------------------------------------------------------- read path
    def account(self) -> BrokerAccount:
        if not self._connected:
            raise BrokerError("The practice broker is not connected.")
        if self.fail_account:
            raise BrokerError("account request timed out")
        if self.live_mode != "PRACTICE":
            self._health = BrokerHealth("MODE_MISMATCH", "Broker mode drifted away from PRACTICE.")
            raise BrokerError("Broker mode drifted away from PRACTICE; TradingOS is fail-closed.")
        return BrokerAccount(balance=self.balance, currency=self.currency, mode="PRACTICE", raw_payload={"balance_id": "fake-balance-1"})

    def assets(self) -> list[dict[str, Any]]:
        if not self._connected:
            raise BrokerError("The practice broker is not connected.")
        return [{"category": "forex", "ticker": "EURUSD", "id": "1", "is_open": True, "payout": 0.82, "precision": 5, "schedule": []}]

    def candles(self, symbol: str, timeframe_seconds: int, count: int) -> list[dict[str, Any]]:
        if not self._connected:
            raise BrokerError("The practice broker is not connected.")
        if symbol in self.fail_candles_on:
            raise BrokerError(f"candle stream for {symbol} failed")
        series = self.candles_by_symbol.get(symbol)
        if series is None:
            raise BrokerError(f"no candle data scripted for {symbol}")
        return series[-count:]

    def positions(self) -> list[dict[str, Any]]:
        if not self._connected:
            raise BrokerError("The practice broker is not connected.")
        return list(self.positions_payload)

    # ------------------------------------------------------------ write path
    def submit_practice_option(self, symbol: str, side: str, amount: float, duration_minutes: int) -> dict[str, Any]:
        if not self._connected:
            raise BrokerError("The practice broker is not connected.")
        if self.fail_submit:
            raise BrokerError("Broker did not accept the practice option order.")
        record = {"broker_order_id": f"fake-order-{len(self.submitted) + 1}", "symbol": symbol, "side": side, "amount": amount, "duration_minutes": duration_minutes, "mode": "PRACTICE"}
        self.submitted.append(record)
        return record


# ------------------------------------------------------------- candle makers

def _epoch(minutes_ago: int, ref: datetime | None = None) -> int:
    ref = ref or datetime.now(UTC)
    return int((ref - timedelta(minutes=minutes_ago)).timestamp())


def sine_candles(count: int = 200, *, base: float = 100.0, amp: float = 1.0, period: int = 12, drift: float = 0.05, timeframe_seconds: int = 60, end_minutes_ago: int = 1) -> list[dict[str, Any]]:
    """Deterministic accepted-by-evaluation series in iqair payload shape.

    The last candle opens `end_minutes_ago` minutes before now, so risk-gate
    freshness checks see live market data.
    """
    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for i in range(count):
        open_epoch = _epoch((count - 1 - i) * timeframe_seconds // 60 + end_minutes_ago, now)
        price = round(base + amp * math.sin(2 * math.pi * i / period) + drift * i, 5)
        out.append({
            "from": open_epoch,
            "to": open_epoch + timeframe_seconds,
            "open": price,
            "max": price + 0.05,
            "min": price - 0.05,
            "close": round(price + 0.01, 5),
            "volume": 1_000 + i,
        })
    return out


def trend_tail_candles(count: int = 200, *, tail: int = 45, base: float = 100.0, amp: float = 1.0, period: int = 12, drift: float = 0.05, rise: float = 0.15, timeframe_seconds: int = 60, end_minutes_ago: int = 1) -> list[dict[str, Any]]:
    """Accepted oscillation history flowing into a decisive smooth uptrend tail.

    The tail continues from the oscillation's last close without a price jump,
    so the series stays realistic while the final closed candle produces a
    deterministic CALL signal (fast EMA above slow EMA, rising).
    Head count must exceed the tail; the function clamps defensively.
    """
    tail = min(tail, max(1, count // 2))
    head = sine_candles(count - tail, base=base, amp=amp, period=period, drift=drift, timeframe_seconds=timeframe_seconds, end_minutes_ago=end_minutes_ago + tail)
    last_open = head[-1]["from"]
    last_close = head[-1]["close"]
    tail_out: list[dict[str, Any]] = []
    for i in range(tail):
        open_epoch = last_open + (i + 1) * timeframe_seconds
        price = round(last_close + rise * (i + 1), 5)
        tail_out.append({
            "from": open_epoch,
            "to": open_epoch + timeframe_seconds,
            "open": price,
            "max": price + 0.05,
            "min": price - 0.05,
            "close": round(price + 0.02, 5),
            "volume": 2_000 + i,
        })
    return head + tail_out


def iqair_position(*, position_id: str, order_id: str, symbol: str = "EURUSD", status: str = "open", pnl: float | None = None, instrument_type: str = "digital-option") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": position_id,
        "external_id": position_id,
        "instrument_type": instrument_type,
        "active": symbol,
        "status": status,
        "open_time": _epoch(30),
        "raw_event": {"order_ids": [order_id]},
    }
    if pnl is not None:
        payload["close_profit"] = pnl
        payload["close_time"] = _epoch(2)
    return payload
