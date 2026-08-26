"""Credential encryption is local and non-plaintext."""

from cryptography.fernet import Fernet

from app.services.credentials import CredentialVault


def test_credential_vault_round_trip_hides_plaintext() -> None:
    vault = CredentialVault(Fernet.generate_key().decode("utf-8"))
    encrypted = vault.encrypt("owner@example.com")
    assert "owner@example.com" not in encrypted
    assert vault.decrypt(encrypted) == "owner@example.com"
