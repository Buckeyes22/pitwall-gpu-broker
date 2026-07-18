from __future__ import annotations

import datetime as dt
import importlib
import os
import sys
from decimal import Decimal
from typing import Any

import httpx
import pytest

from pitwall.core.enums import CapabilitySource, LeaseRenewalPolicy, LeaseState, ProviderType
from pitwall.core.models import Lease, LeaseEndpoints, LeaseReadiness, Provider

_CREATED_AT = dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.UTC)
_TERMINATED_AT = dt.datetime(2026, 5, 28, 12, 5, tzinfo=dt.UTC)


def _env_for_app(**overrides: str) -> dict[str, str]:
    base = {
        "RUNPOD_API_KEY": "test-key",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "REDIS_URL": "redis://localhost:6379/0",
        "PITWALL_ADMIN_SECRET": "s3cret",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clear_api_modules():
    _remove_api_modules()
    yield
    _remove_api_modules()


def _remove_api_modules() -> None:
    for name in [key for key in sys.modules if key.startswith("pitwall.api")]:
        del sys.modules[name]


def _import_app(env: dict[str, str]):
    old = os.environ.copy()
    os.environ.update(env)
    for key in list(os.environ):
        if key not in env and key.startswith(("RUNPOD_", "PITWALL_", "DATABASE_", "REDIS_")):
            del os.environ[key]
    try:
        return importlib.import_module("pitwall.api.app")
    finally:
        os.environ.clear()
        os.environ.update(old)


def _readiness() -> LeaseReadiness:
    return LeaseReadiness(
        runtime_seen_at=_CREATED_AT + dt.timedelta(seconds=10),
        port_mappings_seen_at=_CREATED_AT + dt.timedelta(seconds=11),
        probe_passed_at=_CREATED_AT + dt.timedelta(seconds=20),
        probe_method="ssh_localhost",
    )


def _endpoints(pod_id: str) -> LeaseEndpoints:
    return LeaseEndpoints(
        http={"8000": f"https://{pod_id}-8000.proxy.runpod.net"},
        tcp={"22": {"host": f"{pod_id}.proxy.runpod.net", "port": 19022}},
    )


def _lease(
    lease_id: str,
    pod_id: str,
    *,
    state: LeaseState = LeaseState.ACTIVE,
    cost_accrued_usd: Decimal | None = None,
    terminated_at: dt.datetime | None = None,
    terminated_reason: str | None = None,
) -> Lease:
    return Lease(
        id=lease_id,
        provider_id=f"provider-{lease_id}",
        runpod_pod_id=pod_id,
        state=state,
        created_at=_CREATED_AT,
        expires_at=_CREATED_AT + dt.timedelta(hours=2),
        renewal_policy=LeaseRenewalPolicy.MANUAL,
        endpoints=_endpoints(pod_id),
        readiness=_readiness(),
        cost_accrued_usd=cost_accrued_usd,
        terminated_at=terminated_at,
        terminated_reason=terminated_reason,
    )


def _provider(provider_id: str) -> Provider:
    return Provider(
        id=provider_id,
        capability_id=f"cap-{provider_id}",
        name=f"{provider_id}-name",
        provider_type=ProviderType.POD_LEASE,
        config={"cost": {"per_second_active": "0"}},
        priority=1,
        source=CapabilitySource.API,
        updated_at=_CREATED_AT,
    )


@pytest.mark.anyio
async def test_stop_route_cannot_terminate_unrelated_pods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _import_app(_env_for_app())
    mod.app.state.pool = "pool"
    mod.app.state.redis = None

    from pitwall.api.leases import teardown

    leases = {
        "lease-target": _lease("lease-target", "pod-target"),
        "lease-unrelated": _lease("lease-unrelated", "pod-unrelated"),
    }
    terminated_pods: list[str] = []

    class FakeLeaseRepository:
        def __init__(self, pool: object) -> None:
            assert pool == "pool"

        async def get(self, lease_id: str) -> Lease | None:
            return leases.get(lease_id)

        async def update_state(self, lease_id: str, state: str) -> Lease:
            lease = leases[lease_id]
            updated = _lease(lease.id, lease.runpod_pod_id, state=LeaseState(state))
            leases[lease_id] = updated
            return updated

        async def close_teardown(self, lease_id: str, **kwargs: Any) -> Lease:
            lease = leases[lease_id]
            updated = _lease(
                lease.id,
                lease.runpod_pod_id,
                state=LeaseState(kwargs["state"]),
                cost_accrued_usd=kwargs["cost_accrued_usd"],
                terminated_at=kwargs["terminated_at"],
                terminated_reason=kwargs["terminated_reason"],
            )
            leases[lease_id] = updated
            return updated

    class FakeProviderRepository:
        def __init__(self, pool: object) -> None:
            assert pool == "pool"

        async def get(self, provider_id: str) -> Provider:
            return _provider(provider_id)

    async def fake_terminate_pod(pod_id: str) -> None:
        terminated_pods.append(pod_id)

    monkeypatch.setattr(teardown, "LeaseRepository", FakeLeaseRepository)
    monkeypatch.setattr(teardown, "ProviderRepository", FakeProviderRepository)
    monkeypatch.setattr(teardown, "terminate_pod", fake_terminate_pod)
    monkeypatch.setattr(teardown.dt, "datetime", _FixedDateTime)

    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/leases/lease-target/stop",
            json={"reason": "operator requested"},
        )

    assert response.status_code == 200
    assert terminated_pods == ["pod-target"]
    assert response.json()["runpod_pod_id"] == "pod-target"
    assert response.json()["state"] == "stopped"
    assert leases["lease-unrelated"].state is LeaseState.ACTIVE
    assert leases["lease-unrelated"].runpod_pod_id == "pod-unrelated"


@pytest.mark.anyio
async def test_admin_kill_switch_route_can_terminate_unrelated_pods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "TAILSCALE_OAUTH_CLIENT_ID",
        "TAILSCALE_OAUTH_CLIENT_SECRET",
        "TAILSCALE_TAILNET",
    ):
        monkeypatch.delenv(name, raising=False)
    mod = _import_app(_env_for_app())

    from pitwall.api.admin import emergency, kill_switch

    terminated_pods: list[str] = []
    persisted: list[dict[str, Any]] = []

    async def fake_terminate_all_with_tag(name_prefix: str) -> int:
        assert name_prefix == "pitwall-"
        terminated_pods.extend(["pod-target", "pod-unrelated"])
        return len(terminated_pods)

    async def fake_get_pool() -> object:
        return object()

    async def fake_persist_kill_report(
        _pool: object,
        triggered_at: dt.datetime,
        reason: str,
        actor: str,
        pods_terminated: int,
        total_duration_ms: int,
        errors: list[str],
        **_kwargs: Any,
    ) -> int:
        persisted.append(
            {
                "triggered_at": triggered_at,
                "reason": reason,
                "actor": actor,
                "pods_terminated": pods_terminated,
                "total_duration_ms": total_duration_ms,
                "errors": errors,
            }
        )
        return 1

    monkeypatch.setattr(emergency, "get_pool", fake_get_pool)
    monkeypatch.setattr(emergency, "persist_kill_report", fake_persist_kill_report)
    monkeypatch.setattr(kill_switch, "terminate_all_with_tag", fake_terminate_all_with_tag)

    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/kill-switch",
            headers={"X-Pitwall-Secret": "s3cret"},
            json={"reason": "operator drill"},
        )

    assert response.status_code == 200
    assert terminated_pods == ["pod-target", "pod-unrelated"]
    assert response.json()["pods_terminated"] == 2
    assert persisted == [
        {
            "triggered_at": persisted[0]["triggered_at"],
            "reason": "operator drill",
            "actor": "rest:admin",
            "pods_terminated": 2,
            "total_duration_ms": persisted[0]["total_duration_ms"],
            "errors": [],
        }
    ]


@pytest.mark.anyio
async def test_stop_route_does_not_write_to_kill_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert L15 separation: scoped /v1/leases/{id}/stop does NOT write to kill_log.

    The kill_log is an account-wide audit trail for emergency kill-switch activations.
    Individual lease stops use lease state transitions + Redis pub/sub events, not
    the kill_log. This is the key L15 verb-distinction invariant.
    """
    mod = _import_app(_env_for_app())
    mod.app.state.pool = "pool"
    mod.app.state.redis = None

    from pitwall.api.admin import emergency
    from pitwall.api.leases import teardown

    leases = {
        "lease-target": _lease("lease-target", "pod-target"),
    }
    kill_log_calls: list[dict[str, Any]] = []

    async def fake_persist_kill_report(
        _pool: object,
        triggered_at: dt.datetime,
        reason: str,
        actor: str,
        pods_terminated: int,
        total_duration_ms: int,
        errors: list[str],
        **_kwargs: Any,
    ) -> int:
        kill_log_calls.append(
            {
                "triggered_at": triggered_at,
                "reason": reason,
                "actor": actor,
                "pods_terminated": pods_terminated,
                "total_duration_ms": total_duration_ms,
                "errors": errors,
            }
        )
        return 1

    class FakeLeaseRepository:
        def __init__(self, pool: object) -> None:
            assert pool == "pool"

        async def get(self, lease_id: str) -> Lease | None:
            return leases.get(lease_id)

        async def update_state(self, lease_id: str, state: str) -> Lease:
            lease = leases[lease_id]
            updated = _lease(lease.id, lease.runpod_pod_id, state=LeaseState(state))
            leases[lease_id] = updated
            return updated

        async def close_teardown(self, lease_id: str, **kwargs: Any) -> Lease:
            lease = leases[lease_id]
            updated = _lease(
                lease.id,
                lease.runpod_pod_id,
                state=LeaseState(kwargs["state"]),
                cost_accrued_usd=kwargs["cost_accrued_usd"],
                terminated_at=kwargs["terminated_at"],
                terminated_reason=kwargs["terminated_reason"],
            )
            leases[lease_id] = updated
            return updated

    class FakeProviderRepository:
        def __init__(self, pool: object) -> None:
            assert pool == "pool"

        async def get(self, provider_id: str) -> Provider:
            return _provider(provider_id)

    async def fake_terminate_pod(pod_id: str) -> None:
        pass

    monkeypatch.setattr(emergency, "persist_kill_report", fake_persist_kill_report)
    monkeypatch.setattr(teardown, "LeaseRepository", FakeLeaseRepository)
    monkeypatch.setattr(teardown, "ProviderRepository", FakeProviderRepository)
    monkeypatch.setattr(teardown, "terminate_pod", fake_terminate_pod)
    monkeypatch.setattr(teardown.dt, "datetime", _FixedDateTime)

    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/leases/lease-target/stop",
            json={"reason": "operator requested"},
        )

    assert response.status_code == 200
    assert response.json()["state"] == "stopped"
    assert kill_log_calls == [], (
        "L15 violation: scoped /v1/leases/{id}/stop wrote to kill_log. "
        "Only /v1/admin/kill-switch should write to kill_log."
    )


@pytest.mark.anyio
async def test_kill_switch_route_writes_to_kill_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert L15 separation: account-wide /v1/admin/kill-switch DOES write to kill_log.

    The kill_log is the account-wide audit trail for emergency kill-switch activations.
    This is the counterpart to test_stop_route_does_not_write_to_kill_log - both tests
    together verify the L15 verb-distinction invariant.
    """
    for name in (
        "TAILSCALE_OAUTH_CLIENT_ID",
        "TAILSCALE_OAUTH_CLIENT_SECRET",
        "TAILSCALE_TAILNET",
    ):
        monkeypatch.delenv(name, raising=False)
    mod = _import_app(_env_for_app())

    from pitwall.api.admin import emergency, kill_switch

    kill_log_calls: list[dict[str, Any]] = []

    async def fake_terminate_all_with_tag(name_prefix: str) -> int:
        return 1

    async def fake_get_pool() -> object:
        return object()

    async def fake_persist_kill_report(
        _pool: object,
        triggered_at: dt.datetime,
        reason: str,
        actor: str,
        pods_terminated: int,
        total_duration_ms: int,
        errors: list[str],
        **_kwargs: Any,
    ) -> int:
        kill_log_calls.append(
            {
                "triggered_at": triggered_at,
                "reason": reason,
                "actor": actor,
                "pods_terminated": pods_terminated,
                "total_duration_ms": total_duration_ms,
                "errors": errors,
            }
        )
        return 1

    monkeypatch.setattr(emergency, "get_pool", fake_get_pool)
    monkeypatch.setattr(emergency, "persist_kill_report", fake_persist_kill_report)
    monkeypatch.setattr(kill_switch, "terminate_all_with_tag", fake_terminate_all_with_tag)

    transport = httpx.ASGITransport(app=mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/kill-switch",
            headers={"X-Pitwall-Secret": "s3cret"},
            json={"reason": "L15 audit test"},
        )

    assert response.status_code == 200
    assert len(kill_log_calls) == 1, (
        "L15 violation: /v1/admin/kill-switch did not write to kill_log"
    )
    call = kill_log_calls[0]
    assert call["reason"] == "L15 audit test"
    assert call["actor"] == "rest:admin"
    assert call["errors"] == []


class _FixedDateTime(dt.datetime):
    @classmethod
    def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
        if tz is None:
            return _TERMINATED_AT.replace(tzinfo=None)
        return _TERMINATED_AT.astimezone(tz)
