from __future__ import annotations

import time

from pitwall.webhook_dispatcher import sign, verify


class TestSigner:
    def test_sign_returns_correct_format(self) -> None:
        body = b'{"key": "value"}'
        secret = "test-secret"
        result = sign(body, secret, timestamp=1234567890)
        assert result.startswith("t=1234567890,v1=")
        assert len(result.split("=", 1)[1]) > 16

    def test_sign_produces_different_signatures_for_different_bodies(self) -> None:
        secret = "shared-secret"
        sig1 = sign(b"body1", secret, timestamp=1000)
        sig2 = sign(b"body2", secret, timestamp=1000)
        assert sig1 != sig2

    def test_sign_produces_different_signatures_for_different_secrets(self) -> None:
        body = b"same body"
        sig1 = sign(body, "secret1", timestamp=1000)
        sig2 = sign(body, "secret2", timestamp=1000)
        assert sig1 != sig2

    def test_sign_produces_different_signatures_for_different_timestamps(self) -> None:
        body = b"same body"
        sig1 = sign(body, "secret", timestamp=1000)
        sig2 = sign(body, "secret", timestamp=1001)
        assert sig1 != sig2

    def test_verify_valid_signature(self) -> None:
        body = b'{"a":1}'
        secret = "s"
        now = int(time.time())
        sig = sign(body, secret, timestamp=now)
        assert verify(body, sig, secret, max_age=300) is True

    def test_verify_wrong_secret(self) -> None:
        body = b'{"a":1}'
        sig = sign(body, "s", timestamp=1000)
        assert verify(body, sig, "other", max_age=300) is False

    def test_verify_wrong_body(self) -> None:
        secret = "s"
        sig = sign(b'{"a":1}', secret, timestamp=1000)
        assert verify(b'{"b":2}', sig, secret, max_age=300) is False

    def test_verify_missing_prefix(self) -> None:
        assert verify(b"x", "wrong-header", "s", max_age=300) is False

    def test_verify_invalid_format(self) -> None:
        assert verify(b"x", "t=1000", "s", max_age=300) is False
        assert verify(b"x", "v1=abc", "s", max_age=300) is False

    def test_verify_expired_signature(self) -> None:
        body = b'{"a":1}'
        secret = "s"
        old_timestamp = int(time.time()) - 600
        sig = sign(body, secret, timestamp=old_timestamp)
        assert verify(body, sig, secret, max_age=300) is False

    def test_verify_future_signature_beyond_tolerance(self) -> None:
        body = b'{"a":1}'
        secret = "s"
        future_timestamp = int(time.time()) + 600
        sig = sign(body, secret, timestamp=future_timestamp)
        assert verify(body, sig, secret, max_age=300) is False

    def test_verify_within_max_age(self) -> None:
        body = b'{"a":1}'
        secret = "s"
        recent_timestamp = int(time.time()) - 60
        sig = sign(body, secret, timestamp=recent_timestamp)
        assert verify(body, sig, secret, max_age=300) is True

    def test_verify_with_large_max_age(self) -> None:
        body = b'{"a":1}'
        secret = "s"
        old_timestamp = int(time.time()) - 299
        sig = sign(body, secret, timestamp=old_timestamp)
        assert verify(body, sig, secret, max_age=300) is True
