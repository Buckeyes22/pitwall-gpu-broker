from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from pitwall.api.admin import audit_capability as audit_route
from pitwall.audit.capability import (
    CHECK_CAPABILITY_EXISTS,
    CHECK_READY_TO_INVOKE,
    CHECK_SIXTEEN_CHECK_AUDIT_PASSED,
    REQUIRED_CHECK_NAMES,
    CapabilityAuditCheck,
    CapabilityAuditResult,
)

AuditDelegate = Callable[..., Awaitable[CapabilityAuditResult]]


def _app_with_delegate(
    monkeypatch: pytest.MonkeyPatch,
    delegate: AuditDelegate,
) -> FastAPI:
    app = FastAPI()
    app.include_router(audit_route.router)

    capability_repo = object()
    provider_repo = object()
    pool = object()
    app.dependency_overrides[audit_route._capability_repo] = lambda: capability_repo
    app.dependency_overrides[audit_route._provider_repo] = lambda: provider_repo
    app.dependency_overrides[audit_route._pool] = lambda: pool
    monkeypatch.setattr(audit_route, "audit_capability", delegate)
    return app


def _result(
    capability_name: str,
    *,
    ready_to_invoke: bool,
    failed_check: str | None = None,
) -> CapabilityAuditResult:
    checks: list[CapabilityAuditCheck] = []
    for name in REQUIRED_CHECK_NAMES:
        passed = failed_check != name
        if name == CHECK_READY_TO_INVOKE:
            passed = ready_to_invoke
        checks.append(
            CapabilityAuditCheck(
                name=name,
                passed=passed,
                message=f"{name} {'passed' if passed else 'failed'}",
            )
        )
    return CapabilityAuditResult(
        capability_name=capability_name,
        checks=tuple(checks),
        ready_to_invoke=ready_to_invoke,
    )


async def _post(app: FastAPI, capability_name: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            f"/v1/admin/audit-capability/{capability_name}",
            params={"duration_s": "1"},
        )


@pytest.mark.anyio
async def test_audit_capability_endpoint_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_audit_capability(name: str, **kwargs: Any) -> CapabilityAuditResult:
        calls.append({"name": name, **kwargs})
        return _result(name, ready_to_invoke=True)

    app = _app_with_delegate(monkeypatch, fake_audit_capability)

    response = await _post(app, "embedding.bge-m3")

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_name"] == "embedding.bge-m3"
    assert payload["ready_to_invoke"] is True
    assert all(check["pass"] for check in payload["checks"])
    assert calls[0]["payload"] == {"duration_s": "1"}


@pytest.mark.anyio
async def test_audit_capability_endpoint_reports_forced_failed_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_audit_capability(name: str, **_: Any) -> CapabilityAuditResult:
        return _result(
            name,
            ready_to_invoke=False,
            failed_check=CHECK_SIXTEEN_CHECK_AUDIT_PASSED,
        )

    app = _app_with_delegate(monkeypatch, fake_audit_capability)

    response = await _post(app, "embedding.bge-m3")

    assert response.status_code == 200
    payload = response.json()
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks[CHECK_SIXTEEN_CHECK_AUDIT_PASSED]["pass"] is False
    assert payload["ready_to_invoke"] is False


@pytest.mark.anyio
async def test_audit_capability_endpoint_handles_unknown_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_audit_capability(name: str, **_: Any) -> CapabilityAuditResult:
        return _result(
            name,
            ready_to_invoke=False,
            failed_check=CHECK_CAPABILITY_EXISTS,
        )

    app = _app_with_delegate(monkeypatch, fake_audit_capability)

    response = await _post(app, "missing.capability")

    assert response.status_code == 200
    payload = response.json()
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks[CHECK_CAPABILITY_EXISTS]["pass"] is False
    assert payload["ready_to_invoke"] is False
