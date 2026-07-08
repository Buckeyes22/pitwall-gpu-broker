"""Lease landmine tests — prove MCP requests hit service checks.

These tests verify that MCP lease tool handlers exercise the same
service-layer checks as the REST surface for volume, readiness,
attach-hang, mount-path, stop-scope, and change-set rules.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from pitwall.api.exceptions import LeaseStateConflict
from pitwall.api.leases import launch, teardown
from pitwall.api.schemas.leases import LeasePatch, lease_patch_conflicting_fields
from pitwall.core.enums import (
    CapabilityClass,
    CapabilitySource,
    CostMode,
    LeaseRenewalPolicy,
    LeaseState,
    ProviderType,
)
from pitwall.core.models import (
    Capability,
    Lease,
    LeaseEndpoints,
    LeaseReadiness,
    Provider,
)
from pitwall.leases.state import IllegalLeaseTransitionError, transition_lease_state
from pitwall.mcp.tools import leases as mcp_leases
from pitwall.runpod_client import pods

_CREATED_AT = dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.UTC)
_TERMINATED_AT = dt.datetime(2026, 5, 28, 12, 10, tzinfo=dt.UTC)


def _capability() -> Capability:
    return Capability(
        id="cap_llm_qwen3",
        name="llm.qwen3-32b",
        version="1",
        class_=CapabilityClass.LLM,
        cost_mode=CostMode.PER_SECOND,
        source=CapabilitySource.API,
        created_at=_CREATED_AT,
        updated_at=_CREATED_AT,
    )


def _provider(config: dict[str, Any] | None = None) -> Provider:
    return Provider(
        id="prov_qwen3_h100",
        capability_id="cap_llm_qwen3",
        name="qwen3-h100-pod-us-ca",
        provider_type=ProviderType.POD_LEASE,
        region="US-CA-2",
        cloud_type="SECURE",
        config=config
        or {
            "image_ref": "ghcr.io/acme/pitwall-worker:qwen3",
            "template_name": "pitwall-qwen3-h100",
            "gpu_type_priority": ["NVIDIA H100 80GB HBM3", "NVIDIA L4"],
            "container_disk_gb": 80,
            "volume_id": "vol-model-cache",
            "volume_mount": "/workspace",
            "ports": {"http": [8000], "tcp": [22]},
            "env_vars": {"VLLM_MODEL": "Qwen/Qwen3-32B"},
            "cost": {"per_second_active": "0.002"},
        },
        priority=1,
        source=CapabilitySource.API,
        updated_at=_CREATED_AT,
    )


def _endpoints() -> LeaseEndpoints:
    return LeaseEndpoints(
        http={"8000": "https://pod-target-8000.proxy.runpod.net"},
        tcp={"22": {"host": "pod-target.proxy.runpod.net", "port": 19022}},
    )


def _readiness() -> LeaseReadiness:
    return LeaseReadiness(
        runtime_seen_at=dt.datetime(2026, 5, 28, 12, 0, 18, tzinfo=dt.UTC),
        port_mappings_seen_at=dt.datetime(2026, 5, 28, 12, 0, 19, tzinfo=dt.UTC),
        probe_passed_at=dt.datetime(2026, 5, 28, 12, 0, 34, tzinfo=dt.UTC),
        probe_method="ssh_localhost",
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
        provider_id="prov_qwen3_h100",
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


class _FakePool:
    def acquire(self) -> object:
        raise AssertionError("fake repository should not acquire directly")


class _FakeBudgetGate:
    async def try_launch(self, **_kwargs: Any) -> str:
        return "wkl_landmine"


class _FakeLeaseRepository:
    def __init__(self, pool: object) -> None:
        pass

    async def get(self, lease_id: str) -> Lease:
        return _lease()

    async def update_state(self, lease_id: str, state: str) -> Lease:
        return _lease(LeaseState(state))

    async def update_readiness(self, lease_id: str, readiness: object) -> object:
        return object()

    async def close_teardown(self, lease_id: str, **kwargs: Any) -> Lease:
        return _lease(
            LeaseState(kwargs["state"]),
            cost_accrued_usd=kwargs["cost_accrued_usd"],
            terminated_at=kwargs["terminated_at"],
            terminated_reason=kwargs["terminated_reason"],
        )


class _FakeProviderRepository:
    def __init__(self, pool: object) -> None:
        pass

    async def get(self, provider_id: str) -> Provider:
        return _provider()

    async def patch(self, provider_id: str, **kwargs: Any) -> object:
        return object()


@pytest.mark.anyio
async def test_mcp_lease_pod_volume_check_passes_network_volume_to_pod_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Volume landmine: MCP pitwall_lease_pod passes network_volume_id to pod create."""

    captured_kwargs: dict[str, Any] = {}

    async def fake_ensure_template(*_args: Any, **_kwargs: Any) -> str:
        return "template-vol"

    async def fake_create_pod_with_fallback(**kwargs: Any) -> dict[str, Any]:
        captured_kwargs.update(kwargs)
        return {
            "id": "pod-vol",
            "name": kwargs["name"],
            "readiness": {
                "runtime_seen_at": "2026-05-26T14:00:18Z",
                "port_mappings_seen_at": "2026-05-26T14:00:19Z",
                "probe_passed_at": "2026-05-26T14:00:34Z",
                "probe_method": "ssh_localhost",
            },
        }

    monkeypatch.setattr(launch, "LeaseRepository", _FakeLeaseRepository)
    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)
    monkeypatch.setattr(launch, "create_pod_with_fallback", fake_create_pod_with_fallback)

    result = await launch.run_launch(
        pool=_FakePool(),
        capability=_capability(),
        provider=_provider(),
        budget_gate=_FakeBudgetGate(),
    )

    assert result["pod_id"] == "pod-vol"
    assert captured_kwargs["network_volume_id"] == "vol-model-cache"


@pytest.mark.anyio
async def test_mcp_lease_pod_readiness_check_requires_all_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness landmine: MCP pitwall_lease_pod requires complete readiness before active."""

    events: list[tuple[str, Any]] = []

    class TrackingLeaseRepo(_FakeLeaseRepository):
        async def update_state(self, lease_id: str, state: str) -> Lease:
            events.append(("state", state))
            return await super().update_state(lease_id, state)

        async def update_readiness(self, lease_id: str, readiness: object) -> object:
            events.append(("readiness", readiness))
            return await super().update_readiness(lease_id, readiness)

    async def fake_ensure_template(*_args: Any, **_kwargs: Any) -> str:
        return "template-ready"

    async def fake_create_pod_with_fallback(**kwargs: Any) -> dict[str, Any]:
        return {
            "id": "pod-ready",
            "name": kwargs["name"],
            "readiness": {
                "runtime_seen_at": "2026-05-26T14:00:18Z",
                "port_mappings_seen_at": "2026-05-26T14:00:19Z",
                "probe_passed_at": "2026-05-26T14:00:34Z",
                "probe_method": "ssh_localhost",
            },
        }

    monkeypatch.setattr(launch, "LeaseRepository", TrackingLeaseRepo)
    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)
    monkeypatch.setattr(launch, "create_pod_with_fallback", fake_create_pod_with_fallback)

    result = await launch.run_launch(
        pool=_FakePool(),
        capability=_capability(),
        provider=_provider(),
        budget_gate=_FakeBudgetGate(),
    )

    assert result["pod_id"] == "pod-ready"
    state_transitions = [event[1] for event in events if event[0] == "state"]
    assert state_transitions == ["waiting_runtime", "waiting_probe", "active"]
    readiness_events = [event for event in events if event[0] == "readiness"]
    assert len(readiness_events) == 1
    assert readiness_events[0][1].has_active_signals is True


@pytest.mark.anyio
async def test_mcp_lease_pod_readiness_rejects_incomplete_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness landmine: missing probe signal prevents lease activation."""

    async def fake_ensure_template(*_args: Any, **_kwargs: Any) -> str:
        return "template-nosignal"

    async def fake_create_pod_with_fallback(**kwargs: Any) -> dict[str, Any]:
        return {
            "id": "pod-nosignal",
            "name": kwargs["name"],
            "readiness": {
                "runtime_seen_at": "2026-05-26T14:00:18Z",
                "port_mappings_seen_at": "2026-05-26T14:00:19Z",
            },
        }

    monkeypatch.setattr(launch, "LeaseRepository", _FakeLeaseRepository)
    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)
    monkeypatch.setattr(launch, "create_pod_with_fallback", fake_create_pod_with_fallback)

    with pytest.raises(launch.LaunchConfigError, match="incomplete readiness"):
        await launch.run_launch(
            pool=_FakePool(),
            capability=_capability(),
            provider=_provider(),
            budget_gate=_FakeBudgetGate(),
        )


@pytest.mark.anyio
async def test_mcp_lease_pod_attach_hang_cools_down_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attach-hang landmine: MCP pitwall_lease_pod detects volume attach hang and cools provider."""

    patched: list[tuple[str, dict[str, Any]]] = []
    now = dt.datetime(2026, 5, 28, 12, 30, tzinfo=dt.UTC)

    class TrackingProviderRepo(_FakeProviderRepository):
        async def patch(self, provider_id: str, **kwargs: Any) -> object:
            patched.append((provider_id, kwargs))
            return object()

    async def fake_ensure_template(*_args: Any, **_kwargs: Any) -> str:
        return "template-attach"

    async def fake_create_pod_with_fallback(**kwargs: Any) -> dict[str, Any]:
        raise pods.ProviderAttachHangRecoveryRequested(
            "pod pod-hung volume attach hang exceeded 42s",
            pod_id="pod-hung",
            attach_timeout_s=42.0,
        )

    provider = _provider(
        {
            "image_ref": "ghcr.io/acme/pitwall-worker:qwen3",
            "template_name": "pitwall-qwen3-h100",
            "gpu_type_priority": ["NVIDIA H100 80GB HBM3"],
            "volume_id": "vol-model-cache",
            "constraints": {"max_attach_hang_s": "42"},
            "cost": {"per_second_active": "0.002"},
        }
    )
    monkeypatch.setattr(launch, "ProviderRepository", TrackingProviderRepo)
    monkeypatch.setattr(launch, "_utc_now", lambda: now)
    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)
    monkeypatch.setattr(launch, "create_pod_with_fallback", fake_create_pod_with_fallback)

    result = await launch.run_launch(
        pool=_FakePool(),
        capability=_capability(),
        provider=provider,
        budget_gate=_FakeBudgetGate(),
    )

    assert result["provider_fallback"] is True
    assert result["pod_id"] is None
    assert result["lease_id"] is None
    assert len(patched) == 1
    cooldown_until = patched[0][1]["cooldown_until"]
    assert cooldown_until == now + launch.ATTACH_HANG_PROVIDER_COOLDOWN


@pytest.mark.anyio
async def test_mcp_lease_pod_mount_path_uses_provider_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mount-path landmine: MCP pitwall_lease_pod uses provider-configured mount path."""

    captured_kwargs: dict[str, Any] = {}

    async def fake_ensure_template(*_args: Any, **_kwargs: Any) -> str:
        return "template-mount"

    async def fake_create_pod_with_fallback(**kwargs: Any) -> dict[str, Any]:
        captured_kwargs.update(kwargs)
        return {
            "id": "pod-mount",
            "name": kwargs["name"],
            "readiness": {
                "runtime_seen_at": "2026-05-26T14:00:18Z",
                "port_mappings_seen_at": "2026-05-26T14:00:19Z",
                "probe_passed_at": "2026-05-26T14:00:34Z",
                "probe_method": "ssh_localhost",
            },
        }

    monkeypatch.setattr(launch, "LeaseRepository", _FakeLeaseRepository)
    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)
    monkeypatch.setattr(launch, "create_pod_with_fallback", fake_create_pod_with_fallback)

    provider = _provider(
        {
            "image_ref": "ghcr.io/acme/pitwall-worker:qwen3",
            "template_name": "pitwall-qwen3-h100",
            "gpu_type_priority": ["NVIDIA H100 80GB HBM3"],
            "volume_id": "vol-model-cache",
            "volume_mount": "/data/models",
            "cost": {"per_second_active": "0.002"},
        }
    )

    result = await launch.run_launch(
        pool=_FakePool(),
        capability=_capability(),
        provider=provider,
        budget_gate=_FakeBudgetGate(),
    )

    assert result["pod_id"] == "pod-mount"

    async def fake_ensure_template_mount(*_args: Any, **_kwargs: Any) -> str:
        return "tmpl"

    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template_mount)
    plan = await launch.prepare_lease_launch(
        object(),
        _capability(),
        provider,
    )
    assert plan.template.volume_mount_path == "/data/models"


@pytest.mark.anyio
async def test_mcp_stop_lease_only_terminates_target_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop-scope landmine: MCP pitwall_stop_lease terminates only the targeted pod."""

    terminated_pods: list[str] = []
    state_updates: list[str] = []
    close_kwargs: dict[str, Any] = {}

    class ScopedLeaseRepo(_FakeLeaseRepository):
        async def get(self, lease_id: str) -> Lease:
            return _lease()

        async def update_state(self, lease_id: str, state: str) -> Lease:
            state_updates.append(state)
            return _lease(LeaseState(state))

        async def close_teardown(self, lease_id: str, **kwargs: Any) -> Lease:
            close_kwargs.update(kwargs)
            return _lease(
                LeaseState(kwargs["state"]),
                cost_accrued_usd=kwargs["cost_accrued_usd"],
                terminated_at=kwargs["terminated_at"],
                terminated_reason=kwargs["terminated_reason"],
            )

    async def fake_terminate_pod(pod_id: str) -> None:
        terminated_pods.append(pod_id)

    monkeypatch.setattr(teardown, "LeaseRepository", ScopedLeaseRepo)
    monkeypatch.setattr(teardown, "ProviderRepository", _FakeProviderRepository)
    monkeypatch.setattr(teardown, "terminate_pod", fake_terminate_pod)

    result = await teardown.run_teardown(
        "lease-target",
        pool=_FakePool(),
        reason="mcp_stop",
        now=_TERMINATED_AT,
    )

    assert terminated_pods == ["pod-target"]
    assert state_updates == ["stopping"]
    assert result.lease.state is LeaseState.STOPPED
    assert result.lease.runpod_pod_id == "pod-target"
    assert close_kwargs["terminated_reason"] == "mcp_stop"


@pytest.mark.anyio
async def test_mcp_stop_lease_rejects_non_active_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop-scope landmine: MCP pitwall_stop_lease rejects leases not in ACTIVE state."""

    terminated_pods: list[str] = []

    class StoppingLeaseRepo:
        def __init__(self, pool: object) -> None:
            pass

        async def get(self, lease_id: str) -> Lease:
            return _lease(LeaseState.WAITING_RUNTIME)

    async def fake_terminate_pod(pod_id: str) -> None:
        terminated_pods.append(pod_id)

    monkeypatch.setattr(teardown, "LeaseRepository", StoppingLeaseRepo)
    monkeypatch.setattr(teardown, "ProviderRepository", _FakeProviderRepository)
    monkeypatch.setattr(teardown, "terminate_pod", fake_terminate_pod)

    with pytest.raises(LeaseStateConflict):
        await teardown.run_teardown(
            "lease-target",
            pool=_FakePool(),
            reason="mcp_stop_invalid",
        )

    assert terminated_pods == []


@pytest.mark.anyio
async def test_mcp_stop_lease_idempotent_for_terminal_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop-scope landmine: MCP pitwall_stop_lease is idempotent for already-stopped leases."""

    terminated_pods: list[str] = []
    stopped = _lease(
        LeaseState.STOPPED,
        cost_accrued_usd=Decimal("0.500000"),
        terminated_at=_TERMINATED_AT,
        terminated_reason="already stopped",
    )

    class TerminalLeaseRepo:
        def __init__(self, pool: object) -> None:
            pass

        async def get(self, lease_id: str) -> Lease:
            return stopped

    async def fake_terminate_pod(pod_id: str) -> None:
        terminated_pods.append(pod_id)

    monkeypatch.setattr(teardown, "LeaseRepository", TerminalLeaseRepo)
    monkeypatch.setattr(teardown, "ProviderRepository", _FakeProviderRepository)
    monkeypatch.setattr(teardown, "terminate_pod", fake_terminate_pod)

    result = await teardown.run_teardown("lease-target", pool=_FakePool())

    assert result.lease is stopped
    assert result.event is None
    assert terminated_pods == []


def test_change_set_rejects_multi_axis_patch() -> None:
    """Change-set landmine: multi-axis lease PATCH is rejected."""

    fields = lease_patch_conflicting_fields(
        {
            "image_ref": "ghcr.io/acme/pitwall-worker:sha-1",
            "gpu_type_priority": ["NVIDIA L4"],
            "volume_id": "vol_model_cache",
        }
    )

    assert sorted(fields) == sorted(["image_ref", "gpu_type_priority", "volume_id"])


def test_change_set_allows_single_axis_patch() -> None:
    """Change-set landmine: single-axis lease PATCH is allowed."""

    patch = LeasePatch(
        image_ref="ghcr.io/acme/pitwall-worker:sha-1",
        template_name="pitwall-qwen3",
        renewal_policy="manual",
    )

    assert lease_patch_conflicting_fields(patch) == []


def test_change_set_rejects_image_plus_volume() -> None:
    """Change-set landmine: image + volume together are rejected."""

    fields = lease_patch_conflicting_fields(
        {
            "image_ref": "ghcr.io/acme/pitwall-worker:sha-1",
            "volume_id": "vol_new",
        }
    )

    assert "image_ref" in fields
    assert "volume_id" in fields


def test_change_set_rejects_gpu_plus_volume() -> None:
    """Change-set landmine: GPU + volume together are rejected."""

    fields = lease_patch_conflicting_fields(
        {
            "gpu_type_priority": ["NVIDIA H100 80GB HBM3"],
            "volume_mount": "/data",
        }
    )

    assert "gpu_type_priority" in fields
    assert "volume_mount" in fields


def test_lease_state_transition_enforces_stop_scope() -> None:
    """Stop-scope landmine: cannot jump from STOPPED back to ACTIVE."""

    with pytest.raises(IllegalLeaseTransitionError) as exc_info:
        transition_lease_state(LeaseState.STOPPED, LeaseState.ACTIVE)

    assert exc_info.value.from_state is LeaseState.STOPPED
    assert exc_info.value.to_state is LeaseState.ACTIVE


def test_lease_state_transition_enforces_expired_is_terminal() -> None:
    """Stop-scope landmine: EXPIRED is terminal, no outbound transitions."""

    with pytest.raises(IllegalLeaseTransitionError):
        transition_lease_state(LeaseState.EXPIRED, LeaseState.STOPPING)


def test_mcp_lease_to_response_preserves_lease_state() -> None:
    """Verify MCP response serialization preserves lease state for downstream checks."""

    lease = _lease()
    response = mcp_leases._lease_to_response(lease)

    assert response["state"] == "active"
    assert response["runpod_pod_id"] == "pod-target"
    assert response["provider_id"] == "prov_qwen3_h100"
    assert response["endpoints"] is not None
    assert response["readiness"] is not None


@pytest.mark.anyio
async def test_mcp_lease_pod_attach_timeout_forwarded_to_pod_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attach-hang landmine: provider attach timeout is forwarded to pod create."""

    captured_kwargs: dict[str, Any] = {}

    async def fake_ensure_template(*_args: Any, **_kwargs: Any) -> str:
        return "template-timeout"

    async def fake_create_pod_with_fallback(**kwargs: Any) -> dict[str, Any]:
        captured_kwargs.update(kwargs)
        return {
            "id": "pod-timeout",
            "name": kwargs["name"],
            "readiness": {
                "runtime_seen_at": "2026-05-26T14:00:18Z",
                "port_mappings_seen_at": "2026-05-26T14:00:19Z",
                "probe_passed_at": "2026-05-26T14:00:34Z",
                "probe_method": "ssh_localhost",
            },
        }

    monkeypatch.setattr(launch, "LeaseRepository", _FakeLeaseRepository)
    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)
    monkeypatch.setattr(launch, "create_pod_with_fallback", fake_create_pod_with_fallback)

    provider = _provider(
        {
            "image_ref": "ghcr.io/acme/pitwall-worker:qwen3",
            "template_name": "pitwall-qwen3-h100",
            "gpu_type_priority": ["NVIDIA H100 80GB HBM3"],
            "volume_id": "vol-model-cache",
            "constraints": {"max_attach_hang_s": "60"},
            "cost": {"per_second_active": "0.002"},
        }
    )

    await launch.run_launch(
        pool=_FakePool(),
        capability=_capability(),
        provider=provider,
        budget_gate=_FakeBudgetGate(),
    )

    assert captured_kwargs["volume_attach_timeout_s"] == 60.0


@pytest.mark.anyio
async def test_mcp_stop_lease_closes_cost_before_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop-scope landmine: MCP stop closes cost accounting before marking terminal."""

    close_kwargs: dict[str, Any] = {}

    class CostTrackingLeaseRepo(_FakeLeaseRepository):
        async def close_teardown(self, lease_id: str, **kwargs: Any) -> Lease:
            close_kwargs.update(kwargs)
            return _lease(
                LeaseState(kwargs["state"]),
                cost_accrued_usd=kwargs["cost_accrued_usd"],
                terminated_at=kwargs["terminated_at"],
                terminated_reason=kwargs["terminated_reason"],
            )

    async def fake_terminate_pod(pod_id: str) -> None:
        pass

    monkeypatch.setattr(teardown, "LeaseRepository", CostTrackingLeaseRepo)
    monkeypatch.setattr(teardown, "ProviderRepository", _FakeProviderRepository)
    monkeypatch.setattr(teardown, "terminate_pod", fake_terminate_pod)

    result = await teardown.run_teardown(
        "lease-target",
        pool=_FakePool(),
        reason="mcp_cost_check",
        now=_TERMINATED_AT,
    )

    assert close_kwargs["cost_accrued_usd"] == Decimal("1.200000")
    assert close_kwargs["state"] == "stopped"
    assert close_kwargs["terminated_at"] == _TERMINATED_AT
    assert result.lease.state is LeaseState.STOPPED
