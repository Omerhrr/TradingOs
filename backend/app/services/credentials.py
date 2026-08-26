"""Encryption boundary for local broker credentials."""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


class CredentialConfigurationError(RuntimeError):
    """Raised when local credential encryption has not been configured."""


@dataclass(frozen=True)
class BrokerCredentials:
    email: str
    password: str


class CredentialVault:
    """Thin Fernet wrapper that avoids plaintext credential persistence and logging."""

    def __init__(self, key: str | None) -> None:
        if not key:
            raise CredentialConfigurationError(
                "Set TRADINGOS_CREDENTIAL_ENCRYPTION_KEY before storing any broker credential."
            )
        try:
            self._cipher = Fernet(key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise CredentialConfigurationError("TRADINGOS_CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key.") from exc

    def encrypt(self, value: str) -> str:
        return self._cipher.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, value: str) -> str:
        try:
            return self._cipher.decrypt(value.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise CredentialConfigurationError("Stored broker credentials cannot be decrypted with the configured key.") from exc
