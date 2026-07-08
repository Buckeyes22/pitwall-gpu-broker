from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from pitwall.api.admin import emergency
from pitwall.api.admin.kill_switch import KillReport
from pitwall.api.app import AdminSecretMiddleware

ADMIN_SECRET = "test-admin-secret"


def _report(reason: str) -> KillReport:
    return KillReport(
        triggered_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
        reason=reason,
        tailscale_acl_updated=True,
        devices_removed=2,
        pods_terminated=1,
        total_duration_ms=125,
        errors=[],
    )


def _app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    async def fake_run_kill(
        reason: str,
        actor: str,
        *,
        terminate_compute: bool = True,
    ) -> KillReport:
        calls.append(
            {
                "reason": reason,
                "actor": actor,
                "terminate_compute": terminate_compute,
            }
        )
        return _report(reason)

    app = FastAPI()
    app.add_middleware(AdminSecretMiddleware, secret=ADMIN_SECRET)
    app.include_router(emergency.router)
    monkeypatch.setattr(emergency, "run_kill", fake_run_kill)
    return app, calls


@pytest.mark.anyio
async def test_run_kill_without_tailscale_env_uses_noop_network_sever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "TAILSCALE_OAUTH_CLIENT_ID",
        "TAILSCALE_OAUTH_CLIENT_SECRET",
        "TAILSCALE_TAILNET",
    ):
        monkeypatch.delenv(name, raising=False)

    persisted: list[dict[str, Any]] = []

    async def fake_get_pool() -> object:
        return object()

    async def fake_persist_kill_report(_pool: object, **kwargs: Any) -> int:
        persisted.append(kwargs)
        return 1

    monkeypatch.setattr(emergency, "get_pool", fake_get_pool)
    monkeypatch.setattr(emergency, "persist_kill_report", fake_persist_kill_report)

    report = await emergency.run_kill(
        "no tailnet configured",
        actor="test:no-tailnet",
        terminate_compute=False,
    )

    assert report.tailscale_acl_updated is False
    assert report.devices_removed == 0
    assert report.pods_terminated == 0
    assert report.errors == []
    assert persisted[0]["reason"] == "no tailnet configured"


@pytest.mark.anyio
async def test_kill_switch_route_rejects_missing_and_wrong_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = _app(monkeypatch)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.post(
            "/v1/admin/kill-switch",
            json={"reason": "missing secret", "terminate_compute": True},
        )
        wrong = await client.post(
            "/v1/admin/kill-switch",
            json={"reason": "wrong secret", "terminate_compute": True},
            headers={"X-Pitwall-Secret": "wrong"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert calls == []


@pytest.mark.anyio
async def test_kill_switch_route_rejects_legacy_skypilot_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = _app(monkeypatch)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/admin/kill-switch",
            json={"reason": "legacy alias", "skypilot": False},
            headers={"X-Pitwall-Secret": ADMIN_SECRET},
        )

    assert response.status_code == 422
    assert calls == []


@pytest.mark.anyio
async def test_kill_switch_route_allows_valid_secret_and_returns_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = _app(monkeypatch)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/admin/kill-switch",
            json={"reason": "authorized drill", "terminate_compute": False},
            headers={"X-Pitwall-Secret": ADMIN_SECRET},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["reason"] == "authorized drill"
    assert body["total_duration_ms"] == 125
    assert body["errors"] == []
    assert calls == [
        {
            "reason": "authorized drill",
            "actor": "rest:admin",
            "terminate_compute": False,
        }
    ]
