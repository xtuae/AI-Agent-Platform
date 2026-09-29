"""Fernet at rest, log redaction, secret-bearing reprs (constraint 6)."""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from api.core import crypto
from api.core.logging import _redact
from api.db.models import Message, TenantChannel


def test_encrypt_round_trip() -> None:
    token = "EAAG-fake-meta-token-for-test"
    ct = crypto.encrypt_secret(token)
    assert token.encode() not in ct
    assert crypto.decrypt_secret(ct) == token


def test_ciphertext_is_not_deterministic() -> None:
    assert crypto.encrypt_secret("same") != crypto.encrypt_secret("same")


def test_wrong_key_raises_without_leaking(monkeypatch: pytest.MonkeyPatch) -> None:
    foreign = Fernet(Fernet.generate_key()).encrypt(b"secret")
    with pytest.raises(crypto.DecryptionError) as exc:
        crypto.decrypt_secret(foreign)
    assert "secret" not in str(exc.value).replace("secret could not", "")


def test_log_redaction_backstop() -> None:
    event = {
        "event": "x",
        "wamid": "wamid.1",
        "body": "my address is ...",
        "access_token": "EAAG",
        "tenant_id": "t",
    }
    out = _redact(None, "info", event)
    assert out["body"] == "[redacted]"
    assert out["access_token"] == "[redacted]"
    assert out["wamid"] == "wamid.1"
    assert out["tenant_id"] == "t"


def test_reprs_do_not_expose_secrets_or_bodies() -> None:
    ch = TenantChannel(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        phone_number_id="123",
        access_token_encrypted=b"ciphertext-bytes",
    )
    assert "ciphertext" not in repr(ch)
    msg = Message(id=uuid.uuid4(), wamid="wamid.X", direction="in", body="private text")
    assert "private text" not in repr(msg)
