"""Regression coverage for rejecting NUL bytes before DB-bound API inputs."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.core.enums import LeaseRenewalPolicy, LeaseState
from pitwall.core.models import Lease
from tests.api._contract_helpers import build_app, client_for, override

pytestmark = pytest.mark.anyio

_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)


def _stopped_lease() -> Lease:
    return Lease(
        id="lease_x",
        provider_id="prov_x",
        runpod_pod_id="pod_x",
        state=LeaseState.STOPPED,
        created_at=_NOW,
        expires_at=_NOW + dt.timedelta(hours=1),
        renewal_policy=LeaseRenewalPolicy.MANUAL,
        terminated_at=_NOW,
        terminated_reason="operator requested",
    )


class _WebhookSub:
    def __init__(self) -> None:
        self.id = "whs_x"
        self.consumer = "demo"
        self.webhook_url = "https://example.test/hook"
        self.active = True
        self.created_at = _NOW
        self.updated_at = _NOW


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/jobs/%00"),
        ("GET", "/v1/jobs/%00/status"),
        ("GET", "/v1/jobs/%00/result"),
        ("POST", "/v1/jobs/%00/cancel"),
    ],
)
async def test_job_paths_reject_null_byte_before_repo(
    clear_app_module: None,
    method: str,
    path: str,
) -> None:
    repo = AsyncMock()
    repo.get.return_value = None
    mod = build_app(pool=MagicMock())
    import pitwall.api.routes.jobs as jobs_mod

    jobs_mod._workload_repo = lambda request: repo

    async with client_for(mod) as client:
        resp = await client.request(method, path)

    assert resp.status_code == 422
    repo.get.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("GET", "/v1/leases/%00", None),
        ("PATCH", "/v1/leases/%00", {}),
        ("POST", "/v1/leases/%00/renew", {"extends_minutes": 30}),
        ("POST", "/v1/leases/%00/stop", {"reason": "operator requested"}),
        ("DELETE", "/v1/leases/%00", None),
    ],
)
async def test_lease_paths_reject_null_byte_before_repo_or_teardown(
    clear_app_module: None,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    json_body: dict[str, object] | None,
) -> None:
    repo = AsyncMock()
    repo.get.return_value = None
    teardown = AsyncMock(return_value=SimpleNamespace(lease=_stopped_lease()))
    mod = build_app(pool=MagicMock())
    import pitwall.api.routes.leases as leases_mod

    override(mod, leases_mod._lease_repo, repo)
    monkeypatch.setattr(leases_mod, "run_teardown", teardown)

    async with client_for(mod) as client:
        resp = await client.request(method, path, json=json_body)

    assert resp.status_code == 422
    repo.get.assert_not_awaited()
    teardown.assert_not_awaited()


async def test_lease_stop_rejects_null_byte_reason_before_teardown(
    clear_app_module: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teardown = AsyncMock(return_value=SimpleNamespace(lease=_stopped_lease()))
    mod = build_app(pool=MagicMock())
    import pitwall.api.routes.leases as leases_mod

    monkeypatch.setattr(leases_mod, "run_teardown", teardown)

    async with client_for(mod) as client:
        resp = await client.post("/v1/leases/lease_x/stop", json={"reason": "\x00"})

    assert resp.status_code == 422
    teardown.assert_not_awaited()


async def test_lease_patch_rejects_null_byte_body_field_before_repo(
    clear_app_module: None,
) -> None:
    repo = AsyncMock()
    mod = build_app(pool=MagicMock())
    import pitwall.api.routes.leases as leases_mod

    override(mod, leases_mod._lease_repo, repo)

    async with client_for(mod) as client:
        resp = await client.patch("/v1/leases/lease_x", json={"image": "\x00"})

    assert resp.status_code == 422
    repo.get.assert_not_awaited()


@pytest.mark.parametrize(
    "path",
    [
        "/v1/openai/%00/v1/chat/completions",
        "/v1/openai/llm.qwen3-32b/v1/%00",
    ],
)
async def test_openai_proxy_paths_reject_null_byte_before_repo(
    clear_app_module: None,
    path: str,
) -> None:
    capability_repo = AsyncMock()
    provider_repo = AsyncMock()
    mod = build_app(pool=MagicMock())
    import pitwall.api.routes.openai as openai_mod

    override(mod, openai_mod._capability_repo, capability_repo)
    override(mod, openai_mod._provider_repo, provider_repo)

    async with client_for(mod) as client:
        resp = await client.post(path, json={"model": "qwen", "messages": []})

    assert resp.status_code == 422
    capability_repo.get_by_name.assert_not_awaited()
    provider_repo.list.assert_not_awaited()


async def test_webhook_list_rejects_null_byte_consumer_before_repo(
    clear_app_module: None,
) -> None:
    repo = AsyncMock()
    repo.list.return_value = []
    mod = build_app(pool=MagicMock())
    import pitwall.api.routes.webhook_subscriptions as webhook_mod

    override(mod, webhook_mod._repo, repo)

    async with client_for(mod) as client:
        resp = await client.get("/v1/webhook-subscriptions?consumer=%00")

    assert resp.status_code == 422
    repo.list.assert_not_awaited()


@pytest.mark.parametrize("field", ["consumer", "webhook_url", "hmac_secret"])
async def test_webhook_create_rejects_null_byte_body_fields_before_repo(
    clear_app_module: None,
    field: str,
) -> None:
    repo = AsyncMock()
    repo.create.return_value = _WebhookSub()
    mod = build_app(pool=MagicMock())
    import pitwall.api.routes.webhook_subscriptions as webhook_mod

    override(mod, webhook_mod._repo, repo)
    body = {
        "consumer": "demo",
        "webhook_url": "https://example.test/hook",
        "hmac_secret": "secret",
    }
    body[field] = "\x00"

    async with client_for(mod) as client:
        resp = await client.post("/v1/webhook-subscriptions", json=body)

    assert resp.status_code == 422
    repo.create.assert_not_awaited()


async def test_capability_path_rejects_null_byte_before_repo(
    clear_app_module: None,
) -> None:
    repo = AsyncMock()
    repo.get_by_name.return_value = None
    mod = build_app(pool=MagicMock())
    import pitwall.api.capability_routes as capability_mod

    override(mod, capability_mod._repo, repo)

    async with client_for(mod) as client:
        resp = await client.get("/v1/capabilities/%00")

    assert resp.status_code == 422
    repo.get_by_name.assert_not_awaited()


async def test_capability_list_rejects_null_byte_class_filter_before_repo(
    clear_app_module: None,
) -> None:
    repo = AsyncMock()
    repo.list.return_value = []
    mod = build_app(pool=MagicMock())
    import pitwall.api.capability_routes as capability_mod

    override(mod, capability_mod._repo, repo)

    async with client_for(mod) as client:
        resp = await client.get("/v1/capabilities?class=%00")

    assert resp.status_code == 422
    repo.list.assert_not_awaited()


@pytest.mark.parametrize(
    "path",
    [
        "/v1/providers/%00",
        "/v1/providers/%00/health",
    ],
)
async def test_provider_paths_reject_null_byte_before_repo(
    clear_app_module: None,
    path: str,
) -> None:
    repo = AsyncMock()
    repo.get.return_value = None
    mod = build_app(pool=MagicMock())
    import pitwall.api.provider_routes as provider_mod

    override(mod, provider_mod._repo, repo)

    async with client_for(mod) as client:
        resp = await client.get(path)

    assert resp.status_code == 422
    repo.get.assert_not_awaited()


async def test_provider_list_rejects_null_byte_capability_filter_before_repo(
    clear_app_module: None,
) -> None:
    repo = AsyncMock()
    repo.list.return_value = []
    mod = build_app(pool=MagicMock())
    import pitwall.api.provider_routes as provider_mod

    override(mod, provider_mod._repo, repo)

    async with client_for(mod) as client:
        resp = await client.get("/v1/providers?capability_id=%00")

    assert resp.status_code == 422
    repo.list.assert_not_awaited()


async def test_provider_list_rejects_null_byte_type_filter_before_repo(
    clear_app_module: None,
) -> None:
    repo = AsyncMock()
    repo.list.return_value = []
    mod = build_app(pool=MagicMock())
    import pitwall.api.provider_routes as provider_mod

    override(mod, provider_mod._repo, repo)

    async with client_for(mod) as client:
        resp = await client.get("/v1/providers?provider_type=%00")

    assert resp.status_code == 422
    repo.list.assert_not_awaited()
