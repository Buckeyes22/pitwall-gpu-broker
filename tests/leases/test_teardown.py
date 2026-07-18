from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import pytest

from pitwall.api.leases import teardown
from pitwall.api.routes import leases as lease_routes
from pitwall.api.schemas.leases import LeaseStop
from pitwall.core.enums import CapabilitySource, LeaseRenewalPolicy, LeaseState, ProviderType
from pitwall.core.models import Lease, LeaseEndpoints, LeaseReadiness, Provider

_CREATED_AT = dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.UTC)
_TERMINATED_AT = dt.datetime(2026, 5, 28, 12, 10, tzinfo=dt.UTC)


def _readiness() -> LeaseReadiness:
    return LeaseReadiness(
        runtime_seen_at=dt.datetime(2026, 5, 28, 12, 0, 18, tzinfo=dt.UTC),
        port_mappings_seen_at=dt.datetime(2026, 5, 28, 12, 0, 19, tzinfo=dt.UTC),
        probe_passed_at=dt.datetime(2026, 5, 28, 12, 0, 34, tzinfo=dt.UTC),
        probe_method="ssh_localhost",
    )


def _endpoints() -> LeaseEndpoints:
    return LeaseEndpoints(
        http={"8000": "https://pod-target-8000.proxy.runpod.net"},
        tcp={"22": {"host": "pod-target.proxy.runpod.net", "port": 19022}},
    )


def _lease(
    state: LeaseState = LeaseState.ACTIVE,
    *,
    cost_accrued_usd: Decimal | None = None,
    terminated_at: dt.datetime | None = None,
    terminated_reason: str | None = None,
) -> Lease:
    return Lease(
        id="lease-target",
        provider_id="provider-target",
        runpod_pod_id="pod-target",
        state=state,
        created_at=_CREATED_AT,
        expires_at=_CREATED_AT + dt.timedelta(hours=2),
        renewal_policy=LeaseRenewalPolicy.MANUAL,
        endpoints=_endpoints(),
        readiness=_readiness(),
        cost_accrued_usd=cost_accrued_usd,
        terminated_at=terminated_at,
        terminated_reason=terminated_reason,
    )


def _provider() -> Provider:
    return Provider(
        id="provider-target",
        capability_id="cap-target",
        name="target-provider",
        provider_type=ProviderType.POD_LEASE,
        config={"cost": {"per_second_active": "0.002"}},
        priority=1,
        source=CapabilitySource.API,
        updated_at=_CREATED_AT,
    )


@pytest.mark.anyio
async def test_run_teardown_terminates_only_target_pod_closes_cost_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated_pods: list[str] = []
    state_updates: list[str] = []
    close_kwargs: dict[str, Any] = {}

    class FakeLeaseRepository:
        def __init__(self, pool: object) -> None:
            assert pool == "pool"

        async def get(self, lease_id: str) -> Lease:
            assert lease_id == "lease-target"
            return _lease()

        async def update_state(self, lease_id: str, state: str) -> Lease:
            assert lease_id == "lease-target"
            state_updates.append(state)
            return _lease(LeaseState(state))

        async def close_teardown(self, lease_id: str, **kwargs: Any) -> Lease:
            assert lease_id == "lease-target"
            close_kwargs.update(kwargs)
            return _lease(
                LeaseState(kwargs["state"]),
                cost_accrued_usd=kwargs["cost_accrued_usd"],
                terminated_at=kwargs["terminated_at"],
                terminated_reason=kwargs["terminated_reason"],
            )

    class FakeProviderRepository:
        def __init__(self, pool: object) -> None:
            assert pool == "pool"

        async def get(self, provider_id: str) -> Provider:
            assert provider_id == "provider-target"
            return _provider()

    class FakeRedis:
        def __init__(self) -> None:
            self.published: list[tuple[str, str]] = []

        async def publish(self, channel: str, payload: str) -> int:
            self.published.append((channel, payload))
            return 1

    async def fake_terminate_pod(pod_id: str) -> None:
        terminated_pods.append(pod_id)

    redis = FakeRedis()
    monkeypatch.setattr(teardown, "LeaseRepository", FakeLeaseRepository)
    monkeypatch.setattr(teardown, "ProviderRepository", FakeProviderRepository)
    monkeypatch.setattr(teardown, "terminate_pod", fake_terminate_pod)

    result = await teardown.run_teardown(
        "lease-target",
        pool="pool",
        redis_client=redis,
        reason="operator requested",
        now=_TERMINATED_AT,
    )

    assert terminated_pods == ["pod-target"]
    assert state_updates == ["stopping"]
    assert close_kwargs == {
        "state": "stopped",
        "cost_accrued_usd": Decimal("1.200000"),
        "terminated_at": _TERMINATED_AT,
        "terminated_reason": "operator requested",
    }
    assert result.lease.state is LeaseState.STOPPED
    assert result.published_subscribers == 1
    assert len(redis.published) == 1

    channel, payload = redis.published[0]
    assert channel == teardown.LEASE_TERMINATED_CHANNEL
    assert json.loads(payload) == {
        "cost_accrued_usd": "1.200000",
        "event": "lease.terminated",
        "lease_id": "lease-target",
        "provider_id": "provider-target",
        "runpod_pod_id": "pod-target",
        "state": "stopped",
        "terminated_at": _TERMINATED_AT.isoformat(),
        "terminated_reason": "operator requested",
    }


@pytest.mark.anyio
async def test_run_teardown_can_persist_expired_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated_pods: list[str] = []
    close_kwargs: dict[str, Any] = {}

    class FakeLeaseRepository:
        def __init__(self, pool: object) -> None:
            assert pool == "pool"

        async def get(self, lease_id: str) -> Lease:
            assert lease_id == "lease-target"
            return _lease()

        async def update_state(self, lease_id: str, state: str) -> Lease:
            assert lease_id == "lease-target"
            assert state == "stopping"
            return _lease(LeaseState.STOPPING)

        async def close_teardown(self, lease_id: str, **kwargs: Any) -> Lease:
            assert lease_id == "lease-target"
            close_kwargs.update(kwargs)
            return _lease(
                LeaseState(kwargs["state"]),
                cost_accrued_usd=kwargs["cost_accrued_usd"],
                terminated_at=kwargs["terminated_at"],
                terminated_reason=kwargs["terminated_reason"],
            )

    class FakeProviderRepository:
        def __init__(self, pool: object) -> None:
            assert pool == "pool"

        async def get(self, provider_id: str) -> Provider:
            assert provider_id == "provider-target"
            return _provider()

    class FakeRedis:
        def __init__(self) -> None:
            self.published: list[tuple[str, str]] = []

        async def publish(self, channel: str, payload: str) -> int:
            self.published.append((channel, payload))
            return 1

    async def fake_terminate_pod(pod_id: str) -> None:
        terminated_pods.append(pod_id)

    redis = FakeRedis()
    monkeypatch.setattr(teardown, "LeaseRepository", FakeLeaseRepository)
    monkeypatch.setattr(teardown, "ProviderRepository", FakeProviderRepository)
    monkeypatch.setattr(teardown, "terminate_pod", fake_terminate_pod)

    result = await teardown.run_teardown(
        "lease-target",
        pool="pool",
        redis_client=redis,
        now=_TERMINATED_AT,
        terminal_state=LeaseState.EXPIRED,
    )

    assert terminated_pods == ["pod-target"]
    assert close_kwargs == {
        "state": "expired",
        "cost_accrued_usd": Decimal("1.200000"),
        "terminated_at": _TERMINATED_AT,
        "terminated_reason": "lease_expired",
    }
    assert result.lease.state is LeaseState.EXPIRED
    assert result.published_subscribers == 1

    channel, payload = redis.published[0]
    assert channel == teardown.LEASE_TERMINATED_CHANNEL
    event = json.loads(payload)
    assert event["event"] == "lease.terminated"
    assert event["state"] == "expired"
    assert event["terminated_reason"] == "lease_expired"
    assert event["cost_accrued_usd"] == "1.200000"


@pytest.mark.anyio
async def test_run_teardown_is_idempotent_for_terminal_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated_pods: list[str] = []
    stopped = _lease(
        LeaseState.STOPPED,
        cost_accrued_usd=Decimal("0.500000"),
        terminated_at=_TERMINATED_AT,
        terminated_reason="already stopped",
    )

    class FakeLeaseRepository:
        def __init__(self, _pool: object) -> None:
            pass

        async def get(self, lease_id: str) -> Lease:
            assert lease_id == "lease-target"
            return stopped

    class FakeProviderRepository:
        def __init__(self, _pool: object) -> None:
            pass

    async def fake_terminate_pod(pod_id: str) -> None:
        terminated_pods.append(pod_id)

    monkeypatch.setattr(teardown, "LeaseRepository", FakeLeaseRepository)
    monkeypatch.setattr(teardown, "ProviderRepository", FakeProviderRepository)
    monkeypatch.setattr(teardown, "terminate_pod", fake_terminate_pod)

    result = await teardown.run_teardown("lease-target", pool=object())

    assert result.lease is stopped
    assert result.event is None
    assert result.published_subscribers == 0
    assert terminated_pods == []


def test_close_lease_cost_falls_back_to_existing_cost_without_provider_rate() -> None:
    lease = _lease(cost_accrued_usd=Decimal("0.125000"))

    assert teardown.close_lease_cost(lease, provider=None, terminated_at=_TERMINATED_AT) == Decimal(
        "0.125000"
    )


@pytest.mark.anyio
async def test_stop_route_delegates_to_scoped_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    stopped = _lease(
        LeaseState.STOPPED,
        cost_accrued_usd=Decimal("1.200000"),
        terminated_at=_TERMINATED_AT,
        terminated_reason="operator requested",
    )

    async def fake_run_teardown(
        lease_id: str,
        *,
        pool: object,
        redis_client: object | None,
        reason: str | None,
    ) -> teardown.LeaseTeardownResult:
        calls.append(
            {
                "lease_id": lease_id,
                "pool": pool,
                "redis_client": redis_client,
                "reason": reason,
            }
        )
        return teardown.LeaseTeardownResult(lease=stopped, event=None)

    monkeypatch.setattr(lease_routes, "run_teardown", fake_run_teardown)

    response = await lease_routes.stop_lease(
        "lease-target",
        body=LeaseStop(reason="operator requested"),
        pool="pool",
        redis_client="redis",
    )

    assert calls == [
        {
            "lease_id": "lease-target",
            "pool": "pool",
            "redis_client": "redis",
            "reason": "operator requested",
        }
    ]
    assert response["state"] == "stopped"
    assert response["terminated_at"] == _TERMINATED_AT.isoformat()
    assert response["terminated_reason"] == "operator requested"
    assert response["cost_accrued_usd"] == "1.200000"
