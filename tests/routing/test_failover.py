"""Hermetic tests for spot/preemptible failover with checkpoint resume."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability
from pitwall.core.models import Provider as ProviderRecord
from pitwall.cost.estimator import PerSecondPricing, TaggedPricingModel
from pitwall.providers import (
    ProviderOperationContext,
    ProviderRegistry,
    ProvisionRequest,
    ProvisionResult,
    ReconcileRequest,
    ReconcileResult,
    ResourceStatus,
    StatusRequest,
    StatusResult,
    TeardownRequest,
    TeardownResult,
)
from pitwall.routing.failover import (
    FailoverCapacityMarket,
    FailoverCheckpoint,
    FailoverCheckpointRequest,
    FailoverRequest,
    FailoverResumeRequest,
    FailoverSource,
    FailoverTarget,
    execute_spot_failover,
    is_preempted_status,
    select_on_demand_failover_target,
)

_NOW = dt.datetime(2026, 6, 2, 12, 0, tzinfo=dt.UTC)


class FakeCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1)


@dataclass(slots=True)
class FakeProviderPlugin:
    id: str
    status_result: StatusResult
    name: str = "fake"
    status_requests: list[StatusRequest] = field(default_factory=list)
    provision_requests: list[ProvisionRequest] = field(default_factory=list)
    reconcile_requests: list[ReconcileRequest] = field(default_factory=list)
    teardown_requests: list[TeardownRequest] = field(default_factory=list)

    @property
    def credential_schema(self) -> type[BaseModel]:
        return FakeCredentials

    def pricing_model(
        self,
        capability: Capability,
        provider_record: ProviderRecord,
    ) -> TaggedPricingModel:
        return PerSecondPricing(rate_per_second=Decimal("0.001"))

    async def status(self, request: StatusRequest) -> StatusResult:
        self.status_requests.append(request)
        return self.status_result

    async def provision(self, request: ProvisionRequest) -> ProvisionResult:
        self.provision_requests.append(request)
        return ProvisionResult(
            provider_id=request.provider_record.id,
            external_id=f"vm-{request.provider_record.id}",
            lease_id=f"lease-{request.provider_record.id}",
            raw={"payload": dict(request.payload)},
        )

    async def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        self.reconcile_requests.append(request)
        return ReconcileResult(
            provider_id=request.provider_record.id,
            checked=len(request.external_ids),
            updated=0,
        )

    async def teardown(self, request: TeardownRequest) -> TeardownResult:
        self.teardown_requests.append(request)
        return TeardownResult(
            provider_id=request.provider_record.id,
            lease_id=request.lease_id,
            external_id=None,
        )


def _capability() -> Capability:
    return Capability(
        id="cap_gpu_lease",
        name="gpu.lease",
        version="1",
        class_=CapabilityClass.GPU_LEASE,
        cost_mode=CostMode.PER_SECOND,
        source=CapabilitySource.API,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider_record(provider_id: str, *, priority: int = 1) -> ProviderRecord:
    return ProviderRecord(
        id=provider_id,
        capability_id="cap_gpu_lease",
        name=provider_id,
        provider_type=ProviderType.POD_LEASE,
        config={},
        priority=priority,
        source=CapabilitySource.API,
        updated_at=_NOW,
    )


def _status(
    *,
    provider_id: str = "prov_spot",
    status: ResourceStatus,
    raw: Mapping[str, Any] | None = None,
) -> StatusResult:
    return StatusResult(
        provider_id=provider_id,
        external_id="spot-vm-1",
        status=status,
        raw=raw or {},
    )


def _target(
    provider_id: str,
    *,
    plugin_id: str = "on-demand",
    market: FailoverCapacityMarket = FailoverCapacityMarket.ON_DEMAND,
    price: str,
    latency_ms: str,
) -> FailoverTarget:
    return FailoverTarget(
        provider_plugin_id=plugin_id,
        provider_record=_provider_record(provider_id),
        credentials={"token": f"token-{plugin_id}"},
        market=market,
        gpu="NVIDIA L4",
        price=Decimal(price),
        latency_ms=Decimal(latency_ms),
        provision_payload={"target": provider_id},
    )


def _source() -> FailoverSource:
    return FailoverSource(
        provider_plugin_id="spot",
        provider_record=_provider_record("prov_spot"),
        credentials={"token": "token-spot"},
        external_id="spot-vm-1",
        lease_id="lease-spot-1",
    )


def _registry(
    *,
    source_status: StatusResult,
    target_plugin_id: str = "on-demand",
) -> tuple[ProviderRegistry, FakeProviderPlugin, FakeProviderPlugin]:
    registry = ProviderRegistry()
    source_plugin = FakeProviderPlugin(id="spot", status_result=source_status)
    target_plugin = FakeProviderPlugin(
        id=target_plugin_id,
        status_result=_status(
            provider_id="prov_on_demand",
            status=ResourceStatus.RUNNING,
        ),
    )
    registry.register(source_plugin)
    registry.register(target_plugin)
    return registry, source_plugin, target_plugin


@pytest.mark.anyio
async def test_preempted_status_checkpoints_selects_on_demand_and_resumes() -> None:
    registry, source_plugin, target_plugin = _registry(
        source_status=_status(
            status=ResourceStatus.FAILED,
            raw={"pitwall_preempted": True, "actual_status": "PREEMPTED"},
        )
    )
    checkpoints: list[FailoverCheckpointRequest] = []
    resumes: list[FailoverResumeRequest] = []

    async def checkpoint(request: FailoverCheckpointRequest) -> FailoverCheckpoint:
        checkpoints.append(request)
        return FailoverCheckpoint(token="ckpt-001", state={"offset": 42})

    async def resume(request: FailoverResumeRequest) -> str:
        resumes.append(request)
        return f"resumed:{request.provision_result.external_id}:{request.checkpoint.token}"

    result = await execute_spot_failover(
        FailoverRequest(
            context=ProviderOperationContext(pool=object(), now=_NOW),
            registry=registry,
            capability=_capability(),
            source=_source(),
            targets=[
                _target("prov_expensive", price="0.500000", latency_ms="20"),
                _target("prov_cheap", price="0.100000", latency_ms="200"),
            ],
            checkpoint=checkpoint,
            resume=resume,
            lambda_weight=Decimal("0"),
            request_id="req-failover-1",
        )
    )

    assert result.preempted is True
    assert result.resumed is True
    assert result.target is not None
    assert result.target.provider_record.id == "prov_cheap"
    assert result.checkpoint is not None
    assert result.checkpoint.token == "ckpt-001"
    assert result.resume_result == "resumed:vm-prov_cheap:ckpt-001"
    assert source_plugin.status_requests[0].external_id == "spot-vm-1"
    assert len(target_plugin.provision_requests) == 1
    assert target_plugin.provision_requests[0].provider_record.id == "prov_cheap"
    assert target_plugin.provision_requests[0].payload == {"target": "prov_cheap"}
    assert checkpoints[0].status.raw["pitwall_preempted"] is True
    assert resumes[0].checkpoint.token == "ckpt-001"


@pytest.mark.anyio
async def test_running_status_noops_without_checkpoint_or_resume() -> None:
    registry, _source_plugin, target_plugin = _registry(
        source_status=_status(status=ResourceStatus.RUNNING, raw={"actual_status": "running"})
    )
    called = {"checkpoint": 0, "resume": 0}

    async def checkpoint(request: FailoverCheckpointRequest) -> FailoverCheckpoint:
        called["checkpoint"] += 1
        return FailoverCheckpoint(token="unexpected", state={})

    async def resume(request: FailoverResumeRequest) -> str:
        called["resume"] += 1
        return "unexpected"

    result = await execute_spot_failover(
        FailoverRequest(
            context=ProviderOperationContext(pool=object(), now=_NOW),
            registry=registry,
            capability=_capability(),
            source=_source(),
            targets=[_target("prov_cheap", price="0.100000", latency_ms="200")],
            checkpoint=checkpoint,
            resume=resume,
            lambda_weight=Decimal("0"),
        )
    )

    assert result.preempted is False
    assert result.resumed is False
    assert result.target is None
    assert target_plugin.provision_requests == []
    assert called == {"checkpoint": 0, "resume": 0}


def test_failed_outbid_marker_is_preempted_even_without_provider_annotation() -> None:
    status = _status(
        status=ResourceStatus.FAILED,
        raw={"status": "failed", "status_message": "instance was outbid"},
    )

    assert is_preempted_status(status) is True


def test_plain_failed_status_is_not_treated_as_preemption() -> None:
    status = _status(
        status=ResourceStatus.FAILED,
        raw={"status": "failed", "status_message": "health probe failed"},
    )

    assert is_preempted_status(status) is False


def test_target_selection_ignores_spot_and_preemptible_options() -> None:
    selection = select_on_demand_failover_target(
        [
            _target(
                "prov_spot_candidate",
                market=FailoverCapacityMarket.SPOT,
                price="0.010000",
                latency_ms="1",
            ),
            _target(
                "prov_preemptible_candidate",
                market=FailoverCapacityMarket.PREEMPTIBLE,
                price="0.020000",
                latency_ms="1",
            ),
            _target("prov_on_demand", price="0.500000", latency_ms="100"),
        ],
        lambda_weight=Decimal("0"),
    )

    assert selection.target.provider_record.id == "prov_on_demand"
    assert selection.score.provider_id == "prov_on_demand"


def test_target_selection_uses_arbitrage_lambda_for_latency_tradeoff() -> None:
    low_lambda = select_on_demand_failover_target(
        [
            _target("prov_fast", price="1.000000", latency_ms="10"),
            _target("prov_cheap", price="0.100000", latency_ms="1000"),
        ],
        lambda_weight=Decimal("0"),
    )
    high_lambda = select_on_demand_failover_target(
        [
            _target("prov_fast", price="1.000000", latency_ms="10"),
            _target("prov_cheap", price="0.100000", latency_ms="1000"),
        ],
        lambda_weight=Decimal("0.001"),
    )

    assert low_lambda.target.provider_record.id == "prov_cheap"
    assert high_lambda.target.provider_record.id == "prov_fast"


def test_target_selection_requires_at_least_one_on_demand_target() -> None:
    with pytest.raises(ValueError, match="on-demand"):
        select_on_demand_failover_target(
            [
                _target(
                    "prov_spot_candidate",
                    market=FailoverCapacityMarket.SPOT,
                    price="0.010000",
                    latency_ms="1",
                )
            ],
            lambda_weight=Decimal("0"),
        )


@pytest.mark.anyio
async def test_checkpoint_failure_stops_before_on_demand_provisioning() -> None:
    registry, _source_plugin, target_plugin = _registry(
        source_status=_status(
            status=ResourceStatus.FAILED,
            raw={"pitwall_preempted": True},
        )
    )

    async def checkpoint(request: FailoverCheckpointRequest) -> FailoverCheckpoint:
        raise RuntimeError("checkpoint unavailable")

    async def resume(request: FailoverResumeRequest) -> str:
        return "unexpected"

    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        await execute_spot_failover(
            FailoverRequest(
                context=ProviderOperationContext(pool=object(), now=_NOW),
                registry=registry,
                capability=_capability(),
                source=_source(),
                targets=[_target("prov_cheap", price="0.100000", latency_ms="200")],
                checkpoint=checkpoint,
                resume=resume,
                lambda_weight=Decimal("0"),
            )
        )

    assert target_plugin.provision_requests == []


@pytest.mark.anyio
async def test_resume_failure_tears_down_newly_provisioned_target_and_reraises() -> None:
    registry, _source_plugin, target_plugin = _registry(
        source_status=_status(
            status=ResourceStatus.FAILED,
            raw={"pitwall_preempted": True, "actual_status": "preempted"},
        )
    )

    async def checkpoint(request: FailoverCheckpointRequest) -> FailoverCheckpoint:
        return FailoverCheckpoint(token="ckpt-001", state={"offset": 42})

    async def resume(request: FailoverResumeRequest) -> str:
        raise RuntimeError("resume failed")

    with pytest.raises(RuntimeError, match="resume failed"):
        await execute_spot_failover(
            FailoverRequest(
                context=ProviderOperationContext(pool=object(), now=_NOW),
                registry=registry,
                capability=_capability(),
                source=_source(),
                targets=[_target("prov_cheap", price="0.100000", latency_ms="200")],
                checkpoint=checkpoint,
                resume=resume,
                lambda_weight=Decimal("0"),
                request_id="req-failover-cleanup",
            )
        )

    assert len(target_plugin.provision_requests) == 1
    assert len(target_plugin.teardown_requests) == 1
    teardown_request = target_plugin.teardown_requests[0]
    assert teardown_request.provider_record.id == "prov_cheap"
    assert teardown_request.lease_id == "lease-prov_cheap"
    assert teardown_request.reason == "failover_resume_failed"
    assert isinstance(teardown_request.credentials, FakeCredentials)
    assert teardown_request.credentials.token == "token-on-demand"
