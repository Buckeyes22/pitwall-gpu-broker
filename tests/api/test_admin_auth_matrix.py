"""Task 3: admin-secret auth matrix.

With PITWALL_ADMIN_SECRET set, AdminSecretMiddleware (app.py) must reject every
/v1/admin/* request that lacks the X-Pitwall-Secret header or sends the wrong
value (401), and must NOT 401 when the header is correct (the handler may then
404/422 on a fake id — that is fine; we test the auth gate only).

Admin routes derived from the verified Task 0 inventory.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest

from tests.api._contract_helpers import build_app

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def _gate_client(mod):
    """Client that returns handler exceptions as 500 instead of re-raising.

    Task 3 tests the auth gate only; once the correct secret lets a request
    through, the handler may crash on unconfigured deps — that surfaces as a
    500 response (still != 401 = gate passed), not a test error.
    """
    transport = httpx.ASGITransport(app=mod.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


_SECRET = "test-admin-secret"

# (method, path) for every /v1/admin/* route. Path params use throwaway ids.
_ADMIN_ROUTES = [
    ("POST", "/v1/admin/capabilities"),
    ("PATCH", "/v1/admin/capabilities/cap_x"),
    ("POST", "/v1/admin/capabilities/cap_x/enable"),
    ("POST", "/v1/admin/capabilities/cap_x/disable"),
    ("POST", "/v1/admin/providers"),
    ("PATCH", "/v1/admin/providers/prov_x"),
    ("POST", "/v1/admin/providers/prov_x/enable"),
    ("POST", "/v1/admin/providers/prov_x/disable"),
    ("POST", "/v1/admin/providers/prov_x/hibernate"),
    ("POST", "/v1/admin/audit-capability/embedding.bge-m3"),
    ("POST", "/v1/admin/kill-switch"),
]
_IDS = [f"{m}:{p}" for m, p in _ADMIN_ROUTES]


@pytest.mark.parametrize("method,path", _ADMIN_ROUTES, ids=_IDS)
async def test_missing_secret_is_401(clear_app_module, method: str, path: str) -> None:
    mod = build_app(secret=_SECRET)
    async with _gate_client(mod) as client:
        resp = await client.request(method, path, json={})
    assert resp.status_code == 401


@pytest.mark.parametrize("method,path", _ADMIN_ROUTES, ids=_IDS)
async def test_wrong_secret_is_401(clear_app_module, method: str, path: str) -> None:
    mod = build_app(secret=_SECRET)
    async with _gate_client(mod) as client:
        resp = await client.request(method, path, json={}, headers={"X-Pitwall-Secret": "wrong"})
    assert resp.status_code == 401


@pytest.mark.parametrize("method,path", _ADMIN_ROUTES, ids=_IDS)
async def test_correct_secret_is_not_401(clear_app_module, method: str, path: str) -> None:
    mod = build_app(secret=_SECRET)
    async with _gate_client(mod) as client:
        resp = await client.request(method, path, json={}, headers={"X-Pitwall-Secret": _SECRET})
    assert resp.status_code != 401
