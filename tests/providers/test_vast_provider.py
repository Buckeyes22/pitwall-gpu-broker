from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, LeaseState, ProviderType
from pitwall.core.models import Capability
from pitwall.core.models import Provider as ProviderRecord
from pitwall.cost.estimator import PerSecondPricing
from pitwall.providers import (
    ProviderOperationContext,
    ProvisionRequest,
    ReconcileRequest,
    ResourceStatus,
    StatusRequest,
    TeardownRequest,
    create_default_registry,
)
from pitwall.providers.vast import VastCredentials, VastProvider, VastProviderError

Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class VastHttpFake:
    responses: list[httpx.Response | Handler] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def add(self, response: httpx.Response | Handler) -> None:
        self.responses.append(response)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            return httpx.Response(500, json={"error": "unexpected request"})
        response = self.responses.pop(0)
        if callable(response):
            return response(request)
        return response

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeAcquire:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> FakePool:
        return self._pool

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        return None


@dataclass
class FakePool:
    fetchrow_results: list[dict[str, Any] | None] = field(default_factory=list)
    commands: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.commands.append((query, args))
        if self.fetchrow_results:
            return self.fetchrow_results.pop(0)
        return {
            "id": args[0] if args else "lease-vast",
            "provider_id": "prov_vast_rtx4090",
            "runpod_pod_id": "987654",
            "state": "creating",
            "created_at": dt.datetime(2026, 6, 2, 12, 0, tzinfo=dt.UTC),
            "expires_at": dt.datetime(2026, 6, 2, 14, 0, tzinfo=dt.UTC),
            "renewal_policy": "manual",
            "auto_teardown_on_expiry": True,
            "endpoints": None,
            "readiness": None,
            "cost_accrued_usd": None,
            "last_health_at": None,
            "terminated_at": None,
            "terminated_reason": None,
        }

    async def execute(self, query: str, *args: Any) -> str:
        self.commands.append((query, args))
        return "UPDATE 1"


def _capability() -> Capability:
    now = dt.datetime(2026, 6, 2, 12, 0, tzinfo=dt.UTC)
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


def _provider_record(config: dict[str, Any] | None = None) -> ProviderRecord:
    return ProviderRecord(
        id="prov_vast_rtx4090",
        capability_id="cap_gpu_lease",
        name="vast-rtx4090",
        provider_type=ProviderType.POD_LEASE,
        region="US-CA",
        config=config
        or {
            "ask_id": 12345,
            "create": {
                "image": "pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime",
                "disk": 40,
                "runtype": "ssh_direct",
            },
            "cost": {
                "kind": "per_second",
                "price_per_hour": "0.36",
                "bid_price_per_hour": "0.72",
            },
        },
        priority=1,
        source=CapabilitySource.API,
        updated_at=dt.datetime(2026, 6, 2, 12, 0, tzinfo=dt.UTC),
    )


def _credentials() -> VastCredentials:
    return VastCredentials(api_key="vast-test-key")


def _json(request: httpx.Request) -> dict[str, Any]:
    raw = request.content.decode("utf-8")
    return json.loads(raw, parse_float=Decimal)


def test_default_registry_contains_vast_provider() -> None:
    registry = create_default_registry()

    provider = registry.lookup("vast")

    assert isinstance(provider, VastProvider)
    assert registry.validate_credentials("vast", {"api_key": "vast-test-key"})


def test_vast_credentials_reject_secret_bearing_base_url() -> None:
    registry = create_default_registry()

    with pytest.raises(Exception, match="vast_api_url"):
        registry.validate_credentials(
            "vast",
            {
                "api_key": "vast-test-key",
                "vast_api_url": "https://vast-test-key@console.vast.ai/api/v0",
            },
        )


def test_vast_pricing_model_converts_hourly_price_and_bid_to_per_second() -> None:
    pricing = VastProvider().pricing_model(_capability(), _provider_record())

    assert isinstance(pricing, PerSecondPricing)
    assert pricing.rate_per_second == Decimal("0.0001")
    assert pricing.bid_rate_per_second == Decimal("0.0002")
    assert pricing.upper_bound(_capability(), {}) == Decimal("0.012000")


@pytest.mark.anyio
async def test_vast_provider_provision_accepts_offer_with_bid_using_header_auth() -> None:
    fake = VastHttpFake()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert str(request.url) == "https://console.vast.ai/api/v0/asks/12345/"
        assert request.headers["Authorization"] == "Bearer vast-test-key"
        assert "api_key" not in str(request.url)
        body = _json(request)
        assert body["image"] == "pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime"
        assert body["disk"] == 40
        assert body["price"] == Decimal("0.720000")
        assert body["label"] == "pitwall-prov_vast_rtx4090-req-123"
        return httpx.Response(200, json={"success": True, "new_contract": 987654})

    fake.add(handler)
    provider = VastProvider(transport=fake.transport())

    result = await provider.provision(
        ProvisionRequest(
            context=ProviderOperationContext(pool=object()),
            capability=_capability(),
            provider_record=_provider_record(),
            credentials=_credentials(),
            request_id="req-123",
            payload={"prompt": "do not send to Vast create body"},
        )
    )

    assert result.provider_id == "prov_vast_rtx4090"
    assert result.external_id == "987654"
    assert result.lease_id is None
    assert result.raw["new_contract"] == 987654
    assert len(fake.requests) == 1


@pytest.mark.anyio
async def test_vast_provider_provision_requires_external_id_after_successful_create() -> None:
    fake = VastHttpFake([httpx.Response(200, json={"success": True})])

    with pytest.raises(VastProviderError, match="external id"):
        await VastProvider(transport=fake.transport()).provision(
            ProvisionRequest(
                context=ProviderOperationContext(pool=object()),
                capability=_capability(),
                provider_record=_provider_record(),
                credentials=_credentials(),
                request_id="req-missing-id",
            )
        )

    assert len(fake.requests) == 1


@pytest.mark.anyio
async def test_vast_provider_status_maps_running_instance() -> None:
    fake = VastHttpFake(
        [
            httpx.Response(
                200,
                json={
                    "instances": {
                        "id": 987654,
                        "actual_status": "running",
                        "ssh_host": "ssh.vast.ai",
                        "ssh_port": 12345,
                    }
                },
            )
        ]
    )

    status = await VastProvider(transport=fake.transport()).status(
        StatusRequest(
            context=ProviderOperationContext(pool=object()),
            provider_record=_provider_record(),
            credentials=_credentials(),
            external_id="987654",
        )
    )

    assert status.status is ResourceStatus.RUNNING
    assert status.external_id == "987654"
    assert status.raw["actual_status"] == "running"


@pytest.mark.anyio
async def test_vast_provider_status_maps_preempted_instance_to_failed_safe_state() -> None:
    fake = VastHttpFake(
        [
            httpx.Response(
                200,
                json={
                    "instances": {
                        "id": 987654,
                        "actual_status": "PREEMPTED",
                        "status_msg": "outbid",
                    }
                },
            )
        ]
    )

    status = await VastProvider(transport=fake.transport()).status(
        StatusRequest(
            context=ProviderOperationContext(pool=object()),
            provider_record=_provider_record(),
            credentials=_credentials(),
            external_id="987654",
        )
    )

    assert status.status is ResourceStatus.FAILED
    assert status.raw["pitwall_preempted"] is True
    assert status.raw["pitwall_safe_state"] == LeaseState.FAILED.value


@pytest.mark.anyio
async def test_vast_provider_reconcile_marks_preempted_leases_failed() -> None:
    fake = VastHttpFake(
        [
            httpx.Response(
                200,
                json={"instances": {"id": 987654, "actual_status": "PREEMPTED"}},
            )
        ]
    )
    pool = FakePool()

    result = await VastProvider(transport=fake.transport()).reconcile(
        ReconcileRequest(
            context=ProviderOperationContext(
                pool=pool, now=dt.datetime(2026, 6, 2, 12, 5, tzinfo=dt.UTC)
            ),
            provider_record=_provider_record(),
            credentials=_credentials(),
            external_ids=("987654",),
        )
    )

    assert result.checked == 1
    assert result.updated == 1
    assert result.raw["resources"][0]["pitwall_preempted"] is True
    assert any("terminated_reason" in command[0] for command in pool.commands)
    assert any(LeaseState.FAILED.value in command[1] for command in pool.commands)


@pytest.mark.anyio
async def test_vast_provider_teardown_deletes_instance_and_closes_lease() -> None:
    fake = VastHttpFake()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert str(request.url) == "https://console.vast.ai/api/v0/instances/987654/"
        assert request.headers["Authorization"] == "Bearer vast-test-key"
        return httpx.Response(200, json={"success": True, "msg": "Instance destroyed successfully"})

    fake.add(handler)
    pool = FakePool()

    result = await VastProvider(transport=fake.transport()).teardown(
        TeardownRequest(
            context=ProviderOperationContext(
                pool=pool, now=dt.datetime(2026, 6, 2, 12, 10, tzinfo=dt.UTC)
            ),
            provider_record=_provider_record(),
            credentials=_credentials(),
            lease_id="lease-vast-1",
            reason="operator_stop",
            terminal_state=LeaseState.STOPPED,
        )
    )

    assert result.provider_id == "prov_vast_rtx4090"
    assert result.lease_id == "lease-vast-1"
    assert result.external_id == "987654"
    assert result.raw["success"] is True
    assert any("terminated_reason" in command[0] for command in pool.commands)


@pytest.mark.anyio
async def test_vast_provider_teardown_fails_closed_without_lease_external_id_mapping() -> None:
    fake = VastHttpFake()
    pool = FakePool(fetchrow_results=[None])

    with pytest.raises(VastProviderError, match="external id"):
        await VastProvider(transport=fake.transport()).teardown(
            TeardownRequest(
                context=ProviderOperationContext(pool=pool),
                provider_record=_provider_record(),
                credentials=_credentials(),
                lease_id="lease-without-mapping",
                reason="operator_stop",
                terminal_state=LeaseState.STOPPED,
            )
        )

    assert fake.requests == []
    assert not any("terminated_reason" in command[0] for command in pool.commands)
