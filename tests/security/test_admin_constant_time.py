"""S4: constant-time admin-secret comparison (find→fix proof).

``AdminSecretMiddleware`` originally compared the supplied header to the
configured secret with ``!=``, which short-circuits on the first differing
byte and leaks secret length/prefix through response timing. This module pins
the fix: the comparison MUST go through ``hmac.compare_digest`` and the
timing-leaky ``!=`` form MUST be gone. A through-the-stack table confirms the
gate still accepts the right secret and rejects wrong/missing ones.
"""

from __future__ import annotations

import inspect

import httpx
import pytest

pytestmark = pytest.mark.security


def test_admin_middleware_uses_constant_time_compare() -> None:
    """The secret comparison must be constant-time (find→fix proof)."""
    from pitwall.api import app as app_module

    src = inspect.getsource(app_module.AdminSecretMiddleware)
    assert "hmac.compare_digest" in src, (
        "AdminSecretMiddleware must compare the admin secret with "
        "hmac.compare_digest, not a timing-leaky '!='"
    )
    assert "!= self._secret" not in src, (
        "the timing-leaky '!= self._secret' comparison must be removed"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "expect_401"),
    [
        ("missing", True),
        ("empty", True),
        ("wrong", True),
        ("prefix", True),
        ("correct", False),
    ],
)
async def test_admin_secret_gate_through_the_stack(
    admin_app: tuple[object, str], kind: str, expect_401: bool
) -> None:
    """Right secret passes the gate; missing/empty/wrong/prefix are 401."""
    app, secret = admin_app
    headers = {
        "missing": {},
        "empty": {"X-Pitwall-Secret": ""},
        "wrong": {"X-Pitwall-Secret": "totally-different-value"},
        # same length as `secret` but one byte off — exercises constant-time path
        "prefix": {"X-Pitwall-Secret": secret[:-1] + ("Z" if secret[-1] != "Z" else "Y")},
        "correct": {"X-Pitwall-Secret": secret},
    }[kind]

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/admin/audit-capability/cap_x", headers=headers)

    if expect_401:
        assert resp.status_code == 401
    else:
        assert resp.status_code != 401
