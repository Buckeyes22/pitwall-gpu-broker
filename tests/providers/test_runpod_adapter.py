from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, LeaseState, ProviderType
from pitwall.core.models import Capability, Lease
from pitwall.core.models import Provider as ProviderRecord
from pitwall.providers import (
    ProviderOperationContext,
    ProvisionRequest,
    ReconcileRequest,
    ResourceStatus,
    RunPodCredentials,
    RunPodProvider,
    StatusRequest,
    TeardownRequest,
)
from pitwall.providers import runpod as runpod_provider


def _capability() -> Capability:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
    return Capability(
        id="cap_gpu_lease",
        name="gpu.lease",
        version="1",
        class_=CapabilityClass.GPU_LEASE,
        cost_mode=CostMode.PER_SECOND,
        source=CapabilitySource.API,
        created_at=now,
        updated_at=now,
    )


def _provider_record() -> ProviderRecord:
    return ProviderRecord(
        id="prov_runpod_h100",
        capability_id="cap_gpu_lease",
        name="runpod-h100",
        provider_type=ProviderType.POD_LEASE,
        config={"cost": {"per_second_active": "0.002"}},
        priority=1,
        source=CapabilitySource.API,
        updated_at=datetime(2026, 5, 29, 12, 0, tzinfo=UTC),
    )


def _credentials() -> RunPodCredentials:
    return RunPodCredentials(api_key="test-key")


def _custom_credentials() -> RunPodCredentials:
    return RunPodCredentials(
        api_key="plugin-key",
        graphql_url="https://graphql.runpod.test/graphql",
        rest_api_url="https://rest.runpod.test/v1",
    )


@pytest.mark.anyio
async def test_runpod_provider_delegates_provision_to_existing_launch_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    provider = RunPodProvider()
    context = ProviderOperationContext(pool="pool")
    capability = _capability()
    provider_record = _provider_record()

    async def fake_run_launch(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "provider_id": "prov_runpod_h100",
            "pod_id": "pod-123",
            "lease_id": "lease-123",
            "template_id": "template-123",
        }

    monkeypatch.setattr(runpod_provider.lease_launch, "run_launch", fake_run_launch)

    result = await provider.provision(
        ProvisionRequest(
            context=context,
            capability=capability,
            provider_record=provider_record,
            credentials=_credentials(),
            request_id="req-123",
            extra_env={"MODEL": "qwen"},
            payload={"prompt": "hello"},
            budget_gate="budget",
            idempotency_key="idem-123",
            dry_run=True,
        )
    )

    assert result.provider_id == "prov_runpod_h100"
    assert result.external_id == "pod-123"
    assert result.lease_id == "lease-123"
    assert calls == [
        {
            "pool": "pool",
            "capability": capability,
            "provider": provider_record,
            "request_id": "req-123",
            "extra_env": {"MODEL": "qwen"},
            "payload": {"prompt": "hello"},
            "budget_gate": "budget",
            "idempotency_key": "idem-123",
            "dry_run": True,
            "api_key": "test-key",
            "graphql_url": "https://api.runpod.io/graphql",
        }
    ]


@pytest.mark.anyio
async def test_runpod_provider_threads_validated_credentials_to_operations_with_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    calls: dict[str, dict[str, Any]] = {}
    credentials = _custom_credentials()
    lease = Lease(
        id="lease-123",
        provider_id="prov_runpod_h100",
        runpod_pod_id="pod-123",
        state=LeaseState.STOPPED,
        created_at=datetime(2026, 5, 29, 12, 0, tzinfo=UTC),
        expires_at=datetime(2026, 5, 29, 13, 0, tzinfo=UTC),
        renewal_policy="manual",
        cost_accrued_usd=Decimal("0.500000"),
        terminated_at=datetime(2026, 5, 29, 12, 30, tzinfo=UTC),
        terminated_reason="operator_stop",
    )

    async def fake_run_launch(**kwargs: Any) -> dict[str, Any]:
        calls["launch"] = kwargs
        return {"provider_id": "prov_runpod_h100", "pod_id": "pod-123", "lease_id": "lease-123"}

    async def fake_get_pod(pod_id: str, **kwargs: Any) -> dict[str, Any]:
        calls["status"] = {"pod_id": pod_id, **kwargs}
        return {"id": pod_id, "desiredStatus": "RUNNING"}

    async def fake_get_pods(**kwargs: Any) -> list[dict[str, Any]]:
        calls["list"] = kwargs
        return [{"id": "pod-123", "desiredStatus": "RUNNING"}]

    async def fake_run_teardown(
        lease_id: str,
        *,
        pool: object,
        redis_client: object | None,
        reason: str | None,
        now: datetime | None,
        terminal_state: LeaseState | str,
        **kwargs: Any,
    ) -> runpod_provider.lease_teardown.LeaseTeardownResult:
        calls["teardown"] = {
            "lease_id": lease_id,
            "pool": pool,
            "redis_client": redis_client,
            "reason": reason,
            "now": now,
            "terminal_state": terminal_state,
            **kwargs,
        }
        return runpod_provider.lease_teardown.LeaseTeardownResult(
            lease=lease,
            event={"event": "lease.terminated"},
            published_subscribers=1,
        )

    monkeypatch.setattr(runpod_provider.lease_launch, "run_launch", fake_run_launch)
    monkeypatch.setattr(runpod_provider.runpod_pods, "_get_pod", fake_get_pod)
    monkeypatch.setattr(runpod_provider.runpod_pods, "_get_pods", fake_get_pods)
    monkeypatch.setattr(runpod_provider.lease_teardown, "run_teardown", fake_run_teardown)

    provider = RunPodProvider()
    context = ProviderOperationContext(pool="pool", redis_client="redis")
    provider_record = _provider_record()
    capability = _capability()

    await provider.provision(
        ProvisionRequest(
            context=context,
            capability=capability,
            provider_record=provider_record,
            credentials=credentials,
            payload={"prompt": "hello"},
        )
    )
    await provider.status(
        StatusRequest(
            context=context,
            provider_record=provider_record,
            credentials=credentials,
            external_id="pod-123",
        )
    )
    await provider.reconcile(
        ReconcileRequest(
            context=context,
            provider_record=provider_record,
            credentials=credentials,
        )
    )
    await provider.teardown(
        TeardownRequest(
            context=context,
            provider_record=provider_record,
            credentials=credentials,
            lease_id="lease-123",
            reason="operator_stop",
            terminal_state=LeaseState.STOPPED,
        )
    )

    assert calls["launch"]["api_key"] == "plugin-key"
    assert calls["launch"]["graphql_url"] == "https://graphql.runpod.test/graphql"
    assert calls["launch"]["rest_api_url"] == "https://rest.runpod.test/v1"
    assert calls["status"] == {
        "pod_id": "pod-123",
        "api_key": "plugin-key",
        "rest_api_url": "https://rest.runpod.test/v1",
        "strict_errors": True,
    }
    assert calls["list"] == {
        "api_key": "plugin-key",
        "rest_api_url": "https://rest.runpod.test/v1",
    }
    assert calls["teardown"]["api_key"] == "plugin-key"
    assert calls["teardown"]["rest_api_url"] == "https://rest.runpod.test/v1"


@pytest.mark.anyio
async def test_runpod_provider_threads_credentials_to_template_preparation_and_pod_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, dict[str, Any]] = {}
    provider = RunPodProvider()
    credentials = _custom_credentials()
    context = ProviderOperationContext(pool="pool")
    capability = _capability()
    provider_record = _provider_record()

    async def fake_admit_lease_launch(*args: Any, **kwargs: Any) -> str:
        calls["admit"] = {"args": args, **kwargs}
        return "wkl-123"

    async def fake_prepare_lease_launch(*args: Any, **kwargs: Any):
        calls["prepare"] = {"args": args, **kwargs}
        return runpod_provider.lease_launch.LeaseLaunchPlan(
            template=runpod_provider.lease_launch.LaunchTemplate(
                template_id="template-123",
                template_name="pitwall-template",
                image_ref="ghcr.io/org/worker:sha",
                registry_auth_id=None,
                container_disk_gb=50,
                volume_mount_path="/workspace",
            ),
            env={"PITWALL_PROVIDER_ID": "prov_runpod_h100"},
            workload=runpod_provider.lease_launch.WorkloadConfig(
                name="runpod-h100",
                capability="gpu.lease",
                gpu_types=["NVIDIA L4"],
                container_disk_gb=10,
                min_vcpu=1,
                min_memory_gb=1,
                cloud_type="SECURE",
            ),
            network_volume_id=None,
            data_center_id=None,
            volume_attach_timeout_s=None,
        )

    def fake_pre_lease_persist_callback(**kwargs: Any):
        calls["pre_callback"] = kwargs
        return lambda _pod: None

    async def fake_create_pod_with_fallback(**kwargs: Any) -> dict[str, Any]:
        calls["pod"] = kwargs
        return {"id": "pod-123", "name": kwargs["name"]}

    async def fake_persist_ready_lease(*args: Any, **kwargs: Any) -> None:
        calls["persist"] = {"args": args, **kwargs}

    monkeypatch.setattr(
        runpod_provider.lease_launch,
        "admit_lease_launch",
        fake_admit_lease_launch,
    )
    monkeypatch.setattr(
        runpod_provider.lease_launch,
        "prepare_lease_launch",
        fake_prepare_lease_launch,
    )
    monkeypatch.setattr(
        runpod_provider.lease_launch,
        "_make_pre_lease_persist_callback",
        fake_pre_lease_persist_callback,
    )
    monkeypatch.setattr(
        runpod_provider.lease_launch,
        "_create_pod_with_fallback",
        fake_create_pod_with_fallback,
    )
    monkeypatch.setattr(
        runpod_provider.lease_launch,
        "_persist_ready_lease",
        fake_persist_ready_lease,
    )

    result = await provider.provision(
        ProvisionRequest(
            context=context,
            capability=capability,
            provider_record=provider_record,
            credentials=credentials,
            payload={"prompt": "hello"},
        )
    )

    assert result.external_id == "pod-123"
    assert calls["prepare"]["api_key"] == "plugin-key"
    assert calls["prepare"]["graphql_url"] == "https://graphql.runpod.test/graphql"
    assert calls["pod"]["api_key"] == "plugin-key"
    assert calls["pod"]["rest_api_url"] == "https://rest.runpod.test/v1"


@pytest.mark.anyio
async def test_runpod_provider_status_maps_running_pod_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_pod(pod_id: str, **kwargs: Any) -> dict[str, Any]:
        assert pod_id == "pod-123"
        assert kwargs == {"api_key": "test-key", "strict_errors": True}
        return {"id": "pod-123", "runtime": {"uptimeInSeconds": 30}, "desiredStatus": "RUNNING"}

    monkeypatch.setattr(runpod_provider.runpod_pods, "_get_pod", fake_get_pod)

    status = await RunPodProvider().status(
        StatusRequest(
            context=ProviderOperationContext(pool="pool"),
            provider_record=_provider_record(),
            credentials=_credentials(),
            external_id="pod-123",
        )
    )

    assert status.status is ResourceStatus.RUNNING
    assert status.external_id == "pod-123"
    assert status.raw["desiredStatus"] == "RUNNING"


@pytest.mark.anyio
async def test_runpod_provider_status_marks_missing_pod_terminated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_pod(pod_id: str, **kwargs: Any) -> None:
        assert pod_id == "pod-missing"
        assert kwargs == {"api_key": "test-key", "strict_errors": True}
        return None

    monkeypatch.setattr(runpod_provider.runpod_pods, "_get_pod", fake_get_pod)

    status = await RunPodProvider().status(
        StatusRequest(
            context=ProviderOperationContext(pool="pool"),
            provider_record=_provider_record(),
            credentials=_credentials(),
            external_id="pod-missing",
        )
    )

    assert status.status is ResourceStatus.TERMINATED
    assert status.raw == {}


@pytest.mark.anyio
async def test_runpod_provider_reconcile_checks_requested_pods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    async def fake_get_pod(pod_id: str, **kwargs: Any) -> dict[str, Any]:
        requested.append(pod_id)
        assert kwargs == {"api_key": "test-key", "strict_errors": True}
        return {"id": pod_id, "desiredStatus": "RUNNING"}

    monkeypatch.setattr(runpod_provider.runpod_pods, "_get_pod", fake_get_pod)

    result = await RunPodProvider().reconcile(
        ReconcileRequest(
            context=ProviderOperationContext(pool="pool"),
            provider_record=_provider_record(),
            credentials=_credentials(),
            external_ids=("pod-1", "pod-2"),
        )
    )

    assert requested == ["pod-1", "pod-2"]
    assert result.checked == 2
    assert result.updated == 0
    assert result.raw["resources"] == [
        {"id": "pod-1", "desiredStatus": "RUNNING"},
        {"id": "pod-2", "desiredStatus": "RUNNING"},
    ]


@pytest.mark.anyio
async def test_runpod_provider_reconcile_lists_pods_when_no_ids_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_pods(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs == {"api_key": "test-key"}
        return [{"id": "pod-a"}, {"id": "pod-b"}]

    monkeypatch.setattr(runpod_provider.runpod_pods, "_get_pods", fake_get_pods)

    result = await RunPodProvider().reconcile(
        ReconcileRequest(
            context=ProviderOperationContext(pool="pool"),
            provider_record=_provider_record(),
            credentials=_credentials(),
        )
    )

    assert result.checked == 2
    assert result.raw["resources"] == [{"id": "pod-a"}, {"id": "pod-b"}]


@pytest.mark.anyio
async def test_runpod_provider_delegates_teardown_to_existing_teardown_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    lease = Lease(
        id="lease-123",
        provider_id="prov_runpod_h100",
        runpod_pod_id="pod-123",
        state=LeaseState.STOPPED,
        created_at=datetime(2026, 5, 29, 12, 0, tzinfo=UTC),
        expires_at=datetime(2026, 5, 29, 13, 0, tzinfo=UTC),
        renewal_policy="manual",
        cost_accrued_usd=Decimal("0.500000"),
        terminated_at=datetime(2026, 5, 29, 12, 30, tzinfo=UTC),
        terminated_reason="operator_stop",
    )

    async def fake_run_teardown(
        lease_id: str,
        *,
        pool: object,
        redis_client: object | None,
        reason: str | None,
        now: datetime | None,
        terminal_state: LeaseState | str,
        **kwargs: Any,
    ) -> runpod_provider.lease_teardown.LeaseTeardownResult:
        calls.append(
            {
                "lease_id": lease_id,
                "pool": pool,
                "redis_client": redis_client,
                "reason": reason,
                "now": now,
                "terminal_state": terminal_state,
                **kwargs,
            }
        )
        return runpod_provider.lease_teardown.LeaseTeardownResult(
            lease=lease,
            event={"event": "lease.terminated"},
            published_subscribers=1,
        )

    monkeypatch.setattr(runpod_provider.lease_teardown, "run_teardown", fake_run_teardown)
    now = datetime(2026, 5, 29, 12, 30, tzinfo=UTC)

    result = await RunPodProvider().teardown(
        TeardownRequest(
            context=ProviderOperationContext(pool="pool", redis_client="redis", now=now),
            provider_record=_provider_record(),
            credentials=_credentials(),
            lease_id="lease-123",
            reason="operator_stop",
            terminal_state=LeaseState.STOPPED,
        )
    )

    assert result.provider_id == "prov_runpod_h100"
    assert result.lease_id == "lease-123"
    assert result.external_id == "pod-123"
    assert result.raw == {
        "event": {"event": "lease.terminated"},
        "published_subscribers": 1,
        "state": "stopped",
    }
    assert calls == [
        {
            "lease_id": "lease-123",
            "pool": "pool",
            "redis_client": "redis",
            "reason": "operator_stop",
            "now": now,
            "terminal_state": LeaseState.STOPPED,
            "api_key": "test-key",
        }
    ]
