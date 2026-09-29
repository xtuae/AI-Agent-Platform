"""Fernet encryption for secrets at rest (Meta access tokens). Key from APP_ENCRYPTION_KEY."""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from api.config import get_settings


class DecryptionError(Exception):
    """Ciphertext could not be decrypted with the configured key."""


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(get_settings().app_encryption_key.get_secret_value().encode())


def encrypt_secret(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt_secret(ciphertext: bytes) -> str:
    try:
        return _fernet().decrypt(ciphertext).decode()
    except InvalidToken as exc:
        # Never include the ciphertext or key material in the error.
        raise DecryptionError("secret could not be decrypted with the configured key") from exc
