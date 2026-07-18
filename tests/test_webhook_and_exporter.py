"""Tests for the webhook receiver and cost exporter FastAPI apps.

Add webhook and exporter apps.
"""

from __future__ import annotations

import httpx
import pytest


class _HealthyConnection:
    async def fetchval(self, query: str) -> int:
        assert query == "SELECT 1"
        return 1


class _HealthyAcquire:
    async def __aenter__(self) -> _HealthyConnection:
        return _HealthyConnection()

    async def __aexit__(self, *args: object) -> None:
        return None


class _HealthyPool:
    def acquire(self) -> _HealthyAcquire:
        return _HealthyAcquire()


class _BrokenAcquire:
    async def __aenter__(self) -> None:
        raise RuntimeError("postgresql://user:do-not-leak@db/private")

    async def __aexit__(self, *args: object) -> None:
        return None


class _BrokenPool:
    def acquire(self) -> _BrokenAcquire:
        return _BrokenAcquire()


class _HealthyRedis:
    async def ping(self) -> bool:
        return True


class _BrokenRedis:
    async def ping(self) -> bool:
        raise RuntimeError("redis://:do-not-leak@cache/0")


_WEBHOOK_MONKEYPATCH_ENV = {
    "RUNPOD_API_KEY": None,
    "DATABASE_URL": None,
    "REDIS_URL": None,
}

_COST_EXPORTER_MONKEYPATCH_ENV = {
    "RUNPOD_API_KEY": None,
    "DATABASE_URL": "postgresql://pitwall:pitwall@localhost/pitwall",
    "REDIS_URL": None,
}


@pytest.fixture
def webhook_app():
    monkeypatch = pytest.MonkeyPatch()
    for key, val in _WEBHOOK_MONKEYPATCH_ENV.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    from pitwall.webhook_receiver import app

    monkeypatch.undo()
    return app


@pytest.fixture
def cost_exporter_app():
    monkeypatch = pytest.MonkeyPatch()
    for key, val in _COST_EXPORTER_MONKEYPATCH_ENV.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    from pitwall.cost_exporter.app import app

    monkeypatch.undo()
    return app


@pytest.mark.anyio
async def test_webhook_receiver_healthz(webhook_app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["service"] == "webhook-receiver"


@pytest.mark.anyio
async def test_webhook_receiver_health(webhook_app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


@pytest.mark.anyio
async def test_webhook_receiver_readyz_checks_postgres_and_configured_redis(
    webhook_app,
) -> None:
    webhook_app.state.pool = _HealthyPool()
    webhook_app.state.redis_required = True
    webhook_app.state.redis = _HealthyRedis()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "postgres": {"ok": True},
        "redis": {"ok": True},
    }


@pytest.mark.anyio
async def test_webhook_receiver_readyz_fails_closed_without_leaking_details(
    webhook_app,
) -> None:
    webhook_app.state.pool = _BrokenPool()
    webhook_app.state.redis_required = True
    webhook_app.state.redis = _BrokenRedis()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json() == {
        "ok": False,
        "postgres": {"ok": False, "error": "unavailable"},
        "redis": {"ok": False, "error": "unavailable"},
    }
    assert "do-not-leak" not in resp.text


@pytest.mark.anyio
async def test_cost_exporter_healthz(cost_exporter_app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cost_exporter_app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["service"] == "cost-exporter"


@pytest.mark.anyio
async def test_cost_exporter_health(cost_exporter_app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cost_exporter_app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


@pytest.mark.anyio
async def test_cost_exporter_readyz_checks_postgres(cost_exporter_app) -> None:
    cost_exporter_app.state.pool = _HealthyPool()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cost_exporter_app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "postgres": {"ok": True}}


@pytest.mark.anyio
async def test_cost_exporter_readyz_fails_closed_without_leaking_details(
    cost_exporter_app,
) -> None:
    cost_exporter_app.state.pool = _BrokenPool()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cost_exporter_app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json() == {
        "ok": False,
        "postgres": {"ok": False, "error": "unavailable"},
    }
    assert "do-not-leak" not in resp.text


@pytest.mark.anyio
async def test_cost_exporter_metrics(cost_exporter_app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cost_exporter_app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    text = resp.text
    assert "pitwall_cloud_spend_month_usd" in text
    assert "pitwall_cloud_budget_pct" in text
    assert "pitwall_cloud_spend_month_usd 0" in text


def test_webhook_receiver_module_imports() -> None:
    from pitwall.webhook_receiver import app

    assert app is not None
    assert app.title == "Pitwall Webhook Receiver"


def test_cost_exporter_module_imports() -> None:
    from pitwall.cost_exporter.app import app

    assert app is not None
    assert app.title == "Pitwall Cost Exporter"
