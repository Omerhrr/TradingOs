"""Isolated iqair adapter. The adapter always verifies PRACTICE mode before use."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.services.credentials import BrokerCredentials


class BrokerError(RuntimeError):
    """An operational error returned by the isolated broker adapter."""


class BrokerExecutionDisabled(BrokerError):
    """Raised when a caller attempts broker execution before the practice path is enabled."""


@dataclass(frozen=True)
class BrokerHealth:
    state: str = "DISCONNECTED"
    detail: str = "No authenticated practice broker connection is active."
    last_transition_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class BrokerAccount:
    balance: float | None
    currency: str | None
    mode: str
    raw_payload: dict[str, Any]


class BrokerAdapter:
    """Protocol-like base keeping TradingOS independent from a particular broker library."""

    def health(self) -> BrokerHealth:
        raise NotImplementedError

    def connect_practice(self, credentials: BrokerCredentials) -> BrokerAccount:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def account(self) -> BrokerAccount:
        raise NotImplementedError

    def assets(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def candles(self, symbol: str, timeframe_seconds: int, count: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def submit_practice_option(self, symbol: str, side: str, amount: float, duration_minutes: int) -> dict[str, Any]:
        raise NotImplementedError


class IQAirBrokerAdapter(BrokerAdapter):
    """Single-process iqair adapter with an explicit PRACTICE balance assertion."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._health = BrokerHealth()

    def health(self) -> BrokerHealth:
        return self._health

    def _require_client(self) -> Any:
        if self._client is None:
            raise BrokerError("The practice broker is not connected.")
        return self._client

    def connect_practice(self, credentials: BrokerCredentials) -> BrokerAccount:
        try:
            from iqair.client import IQOptionClient
        except ImportError as exc:
            raise BrokerError("iqair is not installed. Install the broker extra before connecting.") from exc
        self._health = BrokerHealth("CONNECTING", "Opening IQ Option websocket session.")
        client = IQOptionClient(credentials.email, credentials.password)
        connected, reason = client.connect()
        if not connected:
            self._health = BrokerHealth("AUTH_FAILED", "Broker authentication or websocket connection failed.")
            raise BrokerError(f"Practice connection failed: {reason or 'unknown broker error'}")
        client.change_balance("PRACTICE")
        mode = client.get_balance_mode()
        if mode != "PRACTICE":
            try:
                client.close()
            finally:
                self._health = BrokerHealth("MODE_MISMATCH", "Broker did not confirm the PRACTICE balance.")
            raise BrokerError("The broker did not confirm PRACTICE balance mode; no operation was started.")
        self._client = client
        self._health = BrokerHealth("CONNECTED", "Authenticated to the PRACTICE balance.")
        return self.account()

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._health = BrokerHealth("DISCONNECTED", "Broker session closed.")

    def account(self) -> BrokerAccount:
        client = self._require_client()
        mode = client.get_balance_mode()
        if mode != "PRACTICE":
            self._health = BrokerHealth("MODE_MISMATCH", "Broker mode drifted away from PRACTICE.")
            raise BrokerError("Broker mode drifted away from PRACTICE; TradingOS is fail-closed.")
        balance = client.get_balance()
        currency = client.get_currency()
        return BrokerAccount(balance=float(balance) if balance is not None else None, currency=currency, mode=mode, raw_payload={"balance_id": client.get_balance_id()})

    def assets(self) -> list[dict[str, Any]]:
        client = self._require_client()
        entries: list[dict[str, Any]] = []
        for category, assets in client.get_asset_metadata().items():
            for ticker, metadata in assets.items():
                entries.append({"category": category, "ticker": ticker, **metadata})
        return entries

    def candles(self, symbol: str, timeframe_seconds: int, count: int) -> list[dict[str, Any]]:
        client = self._require_client()
        return list(client.get_candles(symbol, timeframe_seconds, count, int(datetime.now(UTC).timestamp())))

    def positions(self) -> list[dict[str, Any]]:
        client = self._require_client()
        positions: list[dict[str, Any]] = []
        for instrument_type in ("turbo-option", "binary-option", "digital-option", "marginal-forex", "marginal-cfd", "marginal-crypto"):
            ok, payload = client.get_positions(instrument_type, limit=100, offset=0)
            if ok:
                for position in payload.get("positions", []):
                    positions.append({"instrument_type": instrument_type, **position})
        return positions

    def submit_practice_option(self, symbol: str, side: str, amount: float, duration_minutes: int) -> dict[str, Any]:
        """Submit only a bounded practice digital-option request; leveraged margin instruments are intentionally unsupported."""
        client = self._require_client()
        if client.get_balance_mode() != "PRACTICE":
            raise BrokerError("Broker rejected: TradingOS only permits a verified PRACTICE balance.")
        normalized_side = side.lower()
        if normalized_side not in {"call", "put"}:
            raise BrokerError("Only CALL or PUT practice option directions are supported.")
        success, order_id = client.buy_digital_spot(symbol, amount, normalized_side, duration_minutes)
        if not success or not order_id:
            raise BrokerError("Broker did not accept the practice option order.")
        return {"broker_order_id": str(order_id), "symbol": symbol, "side": normalized_side, "amount": amount, "duration_minutes": duration_minutes, "mode": "PRACTICE"}
