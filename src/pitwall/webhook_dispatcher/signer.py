"""Timestamped HMAC-SHA256 signing for outbound webhook delivery.

The signed delivery scheme follows the widely-used pattern:
- Signature message: ``{timestamp}.{body}``
- Header format: ``t={timestamp},v1={signature}``
- Verification uses constant-time comparison to prevent timing attacks.
"""

from __future__ import annotations

import hashlib
import hmac
import time


def sign(body: bytes, secret: str, timestamp: int | None = None) -> str:
    """Return a timestamped HMAC-SHA256 signature for body.

    Args:
        body: Raw request body bytes.
        secret: HMAC secret key.
        timestamp: Unix timestamp in seconds. Defaults to current time.

    Returns:
        Signature header value in the form ``t={timestamp},v1={hexdigest}``.
    """
    if timestamp is None:
        timestamp = int(time.time())
    message = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(body: bytes, header: str, secret: str, max_age: int = 300) -> bool:
    """Verify a timestamped HMAC-SHA256 signature.

    Args:
        body: Raw request body bytes.
        header: Signature header value (e.g. ``t=1234567890,v1=abc...``).
        secret: HMAC secret key.
        max_age: Maximum age of signature in seconds. Defaults to 300 (5 minutes).

    Returns:
        True if the signature is valid and the timestamp is within max_age.
    """
    if not header or "," not in header:
        return False

    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    if "t" not in parts or "v1" not in parts:
        return False

    try:
        timestamp = int(parts["t"])
    except ValueError:
        return False

    now = int(time.time())
    if abs(now - timestamp) > max_age:
        return False

    expected = sign(body, secret, timestamp)
    return hmac.compare_digest(expected, header)


__all__ = ["sign", "verify"]
