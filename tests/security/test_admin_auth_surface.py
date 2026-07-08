"""S4: every ``/v1/admin/*`` route is gated by the admin secret.

Enumerates the live app's admin routes (so future admin routes are covered
automatically) and asserts each one returns 401 with a missing or wrong
``X-Pitwall-Secret`` header, and a non-401 status with the correct secret.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from pitwall.api.admin.kill_switch import KillReport
from tests.api._route_helpers import iter_effective_routes
from tests.conftest import _env_for_app, _import_app

pytestmark = pytest.mark.security


def _probe_url(path: str) -> str:
    """Substitute any ``{param}`` placeholders so the path is concrete."""
    return re.sub(r"\{[^}]+\}", "probe", path)


@pytest.mark.anyio
async def test_every_admin_route_requires_secret(admin_app: tuple[object, str]) -> None:
    app, secret = admin_app

    admin_routes = [
        route
        for route in iter_effective_routes(app.routes)
        if getattr(route, "path", "").startswith("/v1/admin") and getattr(route, "methods", None)
    ]
    # The current surface is audit-capability + kill-switch; guard against the
    # enumeration silently going empty (which would make this test vacuous).
    assert len(admin_routes) >= 2, f"expected >=2 admin routes, found {admin_routes}"

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for route in admin_routes:
            method = next(iter(route.methods - {"HEAD", "OPTIONS"}))
            url = _probe_url(route.path)

            missing = await client.request(method, url)
            assert missing.status_code == 401, f"{method} {url} not gated (no secret)"

            wrong = await client.request(method, url, headers={"X-Pitwall-Secret": secret + "x"})
            assert wrong.status_code == 401, f"{method} {url} not gated (wrong secret)"

            allowed = await client.request(method, url, headers={"X-Pitwall-Secret": secret})
            assert allowed.status_code != 401, (
                f"{method} {url} rejected the correct secret (got 401)"
            )


@pytest.mark.anyio
async def test_admin_routes_fail_closed_without_configured_secret(
    clear_app_module: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured admin secret must deny admin routes before handlers run."""
    mod = _import_app(_env_for_app())
    from pitwall.api.admin import emergency

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
        return KillReport(
            triggered_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
            reason=reason,
            tailscale_acl_updated=True,
            devices_removed=2,
            pods_terminated=1,
            total_duration_ms=125,
            errors=[],
        )

    monkeypatch.setattr(emergency, "run_kill", fake_run_kill)

    transport = httpx.ASGITransport(app=mod.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/kill-switch",
            json={"reason": "anonymous kill switch", "terminate_compute": True},
        )

    assert response.status_code == 401
    assert calls == []
