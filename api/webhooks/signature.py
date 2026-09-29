"""X-Hub-Signature-256 verification (constraint 4).

Meta signs the exact raw request body with HMAC-SHA256 keyed by the App Secret and sends
`sha256=<hex digest>`. Verification must run on the raw bytes, before any JSON parsing.
"""

from __future__ import annotations

import hashlib
import hmac

PREFIX = "sha256="


def compute_signature(body: bytes, app_secret: str) -> str:
    return PREFIX + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, header: str | None, app_secret: str | None) -> bool:
    """True only for a present, well-formed, matching signature. Fails closed on no secret."""
    if not app_secret or not header or not header.startswith(PREFIX):
        return False
    expected = compute_signature(body, app_secret)
    # Constant-time; compare lowercase hex so an uppercase-hex sender still verifies.
    return hmac.compare_digest(expected, PREFIX + header[len(PREFIX) :].lower())
