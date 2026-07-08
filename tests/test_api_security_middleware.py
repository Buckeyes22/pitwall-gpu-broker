"""Tests for whole-API auth and inbound rate-limit middleware."""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Mapping
from typing import Any

import httpx
import pytest


def _env_for_app(**overrides: str) -> dict[str, str]:
    base: dict[str, str] = {
        "RUNPOD_API_KEY": "test-key",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "REDIS_URL": "redis://localhost:6379/0",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clear_app_module():
    to_remove = [k for k in sys.modules if k.startswith("pitwall.api")]
    for k in to_remove:
        del sys.modules[k]
    yield
    to_remove = [k for k in sys.modules if k.startswith("pitwall.api")]
    for k in to_remove:
        del sys.modules[k]


def _import_app(env: Mapping[str, str]):
    old = os.environ.copy()
    os.environ.update(env)
    for k in list(os.environ):
        if k not in env and k in (
            "RUNPOD_API_KEY",
            "DATABASE_URL",
            "REDIS_URL",
            "PITWALL_ADMIN_SECRET",
            "PITWALL_API_TOKEN",
            "PITWALL_API_SCOPED_TOKENS",
            "PITWALL_INBOUND_RATE_LIMIT",
            "PITWALL_API_MAX_BODY_BYTES",
        ):
            del os.environ[k]
    try:
        return importlib.import_module("pitwall.api.app")
    finally:
        os.environ.clear()
        os.environ.update(old)


def _add_test_route(mod):
    @mod.app.get("/test-open")
    async def test_open() -> dict[str, bool]:
        return {"ok": True}


pytestmark = pytest.mark.anyio


async def test_api_token_unset_leaves_non_health_routes_open() -> None:
    mod = _import_app(_env_for_app())
    _add_test_route(mod)

    transport = httpx.ASGITransport(app=mod.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/test-open")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_api_token_requires_bearer_on_non_health_routes() -> None:
    mod = _import_app(_env_for_app(PITWALL_API_TOKEN="api-token"))
    _add_test_route(mod)

    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/test-open")
        wrong_scheme = await client.get("/test-open", headers={"Authorization": "Basic api-token"})
        wrong_token = await client.get(
            "/test-open",
            headers={"Authorization": "Bearer wrong"},
        )
        correct = await client.get(
            "/test-open",
            headers={"Authorization": "Bearer api-token"},
        )

    assert missing.status_code == 401
    assert wrong_scheme.status_code == 401
    assert wrong_token.status_code == 401
    assert correct.status_code == 200
    assert correct.json() == {"ok": True}


async def test_api_token_excludes_health_routes() -> None:
    mod = _import_app(_env_for_app(PITWALL_API_TOKEN="api-token"))

    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_scoped_tokens_enforce_read_spend_and_lease_boundaries() -> None:
    scoped_tokens = json.dumps(
        {
            "reader-token": ["read"],
            "spender-token": ["spend"],
            "lease-token": ["lease:mutate"],
        }
    )
    mod = _import_app(_env_for_app(PITWALL_API_SCOPED_TOKENS=scoped_tokens))
    _add_test_route(mod)

    transport = httpx.ASGITransport(app=mod.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        read_ok = await client.get("/test-open", headers={"Authorization": "Bearer reader-token"})
        read_cannot_spend = await client.post(
            "/v1/inference", headers={"Authorization": "Bearer reader-token"}, json={}
        )
        spend_reaches_validation = await client.post(
            "/v1/inference", headers={"Authorization": "Bearer spender-token"}, json={}
        )
        spend_cannot_mutate_lease = await client.patch(
            "/v1/leases/lease_1",
            headers={"Authorization": "Bearer spender-token"},
            json={"auto_teardown_on_expiry": False},
        )
        lease_reaches_validation = await client.patch(
            "/v1/leases/lease_1",
            headers={"Authorization": "Bearer lease-token"},
            json={"image": "immutable:v2"},
        )

    assert read_ok.status_code == 200
    assert read_cannot_spend.status_code == 403
    assert read_cannot_spend.json()["required_scope"] == "spend"
    assert spend_reaches_validation.status_code not in {401, 403}
    assert spend_cannot_mutate_lease.status_code == 403
    assert spend_cannot_mutate_lease.json()["required_scope"] == "lease:mutate"
    assert lease_reaches_validation.status_code not in {401, 403}


async def test_webhook_and_server_admin_scopes_are_distinct() -> None:
    scoped_tokens = json.dumps(
        {
            "reader-token": ["read"],
            "webhook-token": ["webhook:admin"],
            "server-token": ["server:admin"],
        }
    )
    mod = _import_app(
        _env_for_app(
            PITWALL_API_SCOPED_TOKENS=scoped_tokens,
            PITWALL_ADMIN_SECRET="admin-secret",
        )
    )

    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        reader_webhook = await client.get(
            "/v1/webhook-subscriptions/probe",
            headers={"Authorization": "Bearer reader-token"},
        )
        webhook_allowed = await client.get(
            "/v1/webhook-subscriptions/probe",
            headers={"Authorization": "Bearer webhook-token"},
        )
        webhook_server = await client.get(
            "/v1/admin/probe",
            headers={
                "Authorization": "Bearer webhook-token",
                "X-Pitwall-Secret": "admin-secret",
            },
        )
        server_allowed = await client.get(
            "/v1/admin/probe",
            headers={
                "Authorization": "Bearer server-token",
                "X-Pitwall-Secret": "admin-secret",
            },
        )

    assert reader_webhook.status_code == 403
    assert webhook_allowed.status_code in {404, 405, 422}
    assert webhook_server.status_code == 403
    assert server_allowed.status_code == 404


def test_invalid_scoped_token_configuration_fails_closed() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _import_app(_env_for_app(PITWALL_API_SCOPED_TOKENS='{"token":["root"]}'))
    assert exc_info.value.code == os.EX_CONFIG


def test_openapi_declares_enforced_scope_contract() -> None:
    mod = _import_app(_env_for_app(PITWALL_API_TOKEN="api-token"))
    schema = mod.app.openapi()

    assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
    assert schema["paths"]["/v1/leases/{lease_id}/renew"]["post"]["x-required-scope"] == (
        "lease:mutate"
    )
    assert schema["paths"]["/v1/inference"]["post"]["x-required-scope"] == "spend"
    assert schema["paths"]["/v1/capabilities"]["get"]["x-required-scope"] == "read"
    assert "security" not in schema["paths"]["/v1/health"]["get"]
    errors = schema["paths"]["/v1/inference"]["post"]["responses"]
    assert errors["401"]["headers"]["WWW-Authenticate"]["schema"]["type"] == "string"
    assert errors["429"]["headers"]["Retry-After"]["schema"]["type"] == "string"
    assert errors["413"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }


async def test_inbound_rate_limit_returns_429_after_threshold() -> None:
    mod = _import_app(_env_for_app(PITWALL_INBOUND_RATE_LIMIT="2/60s"))
    _add_test_route(mod)

    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/test-open")
        second = await client.get("/test-open")
        limited = await client.get("/test-open")

    assert first.status_code == 200
    assert second.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "30"
    assert limited.json()["detail"] == "rate limit exceeded"


async def test_inbound_rate_limit_excludes_health_routes() -> None:
    mod = _import_app(_env_for_app(PITWALL_INBOUND_RATE_LIMIT="1/60s"))
    _add_test_route(mod)

    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/test-open")
        limited = await client.get("/test-open")
        health = await client.get("/healthz")

    assert first.status_code == 200
    assert limited.status_code == 429
    assert health.status_code == 200
    assert health.json()["ok"] is True


async def test_inbound_rate_limit_keys_by_bearer_token_when_set() -> None:
    mod = _import_app(
        _env_for_app(
            PITWALL_API_TOKEN="api-token",
            PITWALL_INBOUND_RATE_LIMIT="1/60s",
        )
    )
    _add_test_route(mod)

    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/test-open",
            headers={"Authorization": "Bearer api-token"},
        )
        limited = await client.get(
            "/test-open",
            headers={"Authorization": "Bearer api-token"},
        )

    assert first.status_code == 200
    assert limited.status_code == 429


def test_invalid_inbound_rate_limit_fails_closed() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _import_app(_env_for_app(PITWALL_INBOUND_RATE_LIMIT="invalid"))
    assert exc_info.value.code == os.EX_CONFIG


async def test_request_body_limit_rejects_before_route_parsing() -> None:
    mod = _import_app(_env_for_app(PITWALL_API_MAX_BODY_BYTES="16"))
    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/inference", content=b"x" * 17)
    assert response.status_code == 413
    assert response.json() == {"error": "request_rejected", "detail": "request body too large"}


async def test_request_body_limit_rejects_chunked_body_before_route() -> None:
    mod = _import_app(_env_for_app(PITWALL_API_MAX_BODY_BYTES="16"))
    route_called = False

    @mod.app.post("/chunked-probe")
    async def chunked_probe() -> dict[str, bool]:
        nonlocal route_called
        route_called = True
        return {"ok": True}

    async def body() -> Any:
        yield b"x" * 10
        yield b"x" * 7

    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/chunked-probe", content=body())
    assert response.status_code == 413
    assert route_called is False


async def test_readyz_reports_dependency_state_without_error_details() -> None:
    mod = _import_app(_env_for_app())

    class HealthyConnection:
        async def fetchval(self, query: str) -> int:
            assert query == "SELECT 1"
            return 1

    class Acquire:
        async def __aenter__(self) -> HealthyConnection:
            return HealthyConnection()

        async def __aexit__(self, *args: Any) -> None:
            return None

    class HealthyPool:
        def acquire(self) -> Acquire:
            return Acquire()

    class HealthyRedis:
        async def ping(self) -> bool:
            return True

    mod.app.state.pool = HealthyPool()
    mod.app.state.redis = HealthyRedis()
    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "postgres": {"ok": True},
        "redis": {"ok": True},
    }


async def test_readyz_fails_closed_and_redacts_dependency_exception() -> None:
    mod = _import_app(_env_for_app())

    class BrokenAcquire:
        async def __aenter__(self) -> None:
            raise RuntimeError("postgresql://user:super-secret@db/private")

        async def __aexit__(self, *args: Any) -> None:
            return None

    class BrokenPool:
        def acquire(self) -> BrokenAcquire:
            return BrokenAcquire()

    class BrokenRedis:
        async def ping(self) -> bool:
            raise RuntimeError("redis://:super-secret@cache/0")

    mod.app.state.pool = BrokenPool()
    mod.app.state.redis = BrokenRedis()
    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "postgres": {"ok": False, "error": "unavailable"},
        "redis": {"ok": False, "error": "unavailable"},
    }
    assert "super-secret" not in response.text
