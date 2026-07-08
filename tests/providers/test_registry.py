from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability
from pitwall.core.models import Provider as ProviderRecord
from pitwall.cost.estimator import GpuHourPricing, PerVmSecondPricing, TaggedPricingModel
from pitwall.providers import (
    CredentialValidationError,
    DuplicateProviderError,
    Provider,
    ProviderNotRegisteredError,
    ProviderOperationContext,
    ProviderRegistry,
    ProvisionRequest,
    ProvisionResult,
    ReconcileRequest,
    ReconcileResult,
    ResourceStatus,
    RunPodCredentials,
    RunPodProvider,
    StatusRequest,
    StatusResult,
    TeardownRequest,
    TeardownResult,
    create_default_registry,
)


def _capability(cost_mode: CostMode = CostMode.PER_SECOND) -> Capability:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
    return Capability(
        id="cap_gpu_lease",
        name="gpu.lease",
        version="1",
        class_=CapabilityClass.GPU_LEASE,
        cost_mode=cost_mode,
        source=CapabilitySource.API,
        created_at=now,
        updated_at=now,
    )


def _provider_record(config: dict[str, Any] | None = None) -> ProviderRecord:
    return ProviderRecord(
        id="prov_runpod_h100",
        capability_id="cap_gpu_lease",
        name="runpod-h100",
        provider_type=ProviderType.POD_LEASE,
        config=config or {"cost": {"per_second_active": "0.002"}},
        priority=1,
        source=CapabilitySource.API,
        updated_at=datetime(2026, 5, 29, 12, 0, tzinfo=UTC),
    )


class ExampleCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1)
    account_id: str


class ExampleProvider:
    id = "example"
    name = "Example Cloud"
    credential_schema = ExampleCredentials

    def pricing_model(
        self,
        capability: Capability,
        provider_record: ProviderRecord,
    ) -> TaggedPricingModel:
        return GpuHourPricing(per_second_active=Decimal("0.001"))

    async def provision(self, request: ProvisionRequest) -> ProvisionResult:
        return ProvisionResult(
            provider_id=request.provider_record.id,
            external_id="vm-123",
            lease_id="lease-123",
            raw={"provider": self.id},
        )

    async def status(self, request: StatusRequest) -> StatusResult:
        return StatusResult(
            provider_id=request.provider_record.id,
            external_id=request.external_id,
            status=ResourceStatus.RUNNING,
            raw={"provider": self.id},
        )

    async def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        return ReconcileResult(
            provider_id=request.provider_record.id,
            checked=len(request.external_ids),
            updated=0,
            raw={"provider": self.id},
        )

    async def teardown(self, request: TeardownRequest) -> TeardownResult:
        return TeardownResult(
            provider_id=request.provider_record.id,
            lease_id=request.lease_id,
            external_id="vm-123",
            raw={"provider": self.id},
        )


def test_provider_protocol_accepts_complete_plugin() -> None:
    provider = ExampleProvider()

    assert isinstance(provider, Provider)


def test_registry_registers_and_looks_up_provider_by_id() -> None:
    registry = ProviderRegistry()
    provider = ExampleProvider()

    returned = registry.register(provider)

    assert returned is provider
    assert registry.lookup("example") is provider
    assert registry.ids == ("example",)


def test_registry_rejects_duplicate_provider_id() -> None:
    registry = ProviderRegistry()
    registry.register(ExampleProvider())

    with pytest.raises(DuplicateProviderError, match="example"):
        registry.register(ExampleProvider())


def test_registry_rejects_blank_provider_id() -> None:
    class BlankIdProvider(ExampleProvider):
        id = " "

    registry = ProviderRegistry()

    with pytest.raises(ValueError, match="provider id"):
        registry.register(BlankIdProvider())


def test_registry_raises_for_unknown_provider_lookup() -> None:
    registry = ProviderRegistry()

    with pytest.raises(ProviderNotRegisteredError, match="missing"):
        registry.lookup("missing")


def test_registry_validates_credentials_against_provider_schema() -> None:
    registry = ProviderRegistry()
    registry.register(ExampleProvider())

    credentials = registry.validate_credentials(
        "example",
        {"token": "tok_123", "account_id": "acct_123"},
    )

    assert isinstance(credentials, ExampleCredentials)
    assert credentials.account_id == "acct_123"


def test_registry_credential_validation_error_is_safe_to_log() -> None:
    registry = ProviderRegistry()
    registry.register(ExampleProvider())

    with pytest.raises(CredentialValidationError) as raised:
        registry.validate_credentials(
            "example",
            {
                "token": "tok_super_secret",
                "account_id": "acct_123",
                "unexpected": "also_secret",
            },
        )

    error = raised.value
    assert error.provider_id == "example"
    assert error.fields == ("unexpected",)
    assert "tok_super_secret" not in str(error)
    assert "also_secret" not in str(error)


def test_default_registry_contains_runpod_reference_provider() -> None:
    registry = create_default_registry()
    provider = registry.lookup("runpod")

    assert isinstance(provider, RunPodProvider)
    assert registry.validate_credentials("runpod", {"api_key": "test-key"})


def test_registry_exposes_provider_credential_json_schema() -> None:
    registry = create_default_registry()

    schema = registry.credential_json_schema("runpod")

    api_key_schema = schema["properties"]["api_key"]
    assert api_key_schema["format"] == "password"
    assert api_key_schema["writeOnly"] is True


def test_runpod_credentials_reject_secret_bearing_urls() -> None:
    registry = create_default_registry()

    with pytest.raises(CredentialValidationError, match="graphql_url") as raised:
        registry.validate_credentials(
            "runpod",
            {
                "api_key": "test-key",
                "graphql_url": "https://token@example.com/graphql",
            },
        )

    assert raised.value.fields == ("graphql_url",)


def test_runpod_pricing_model_uses_tagged_pricing_union() -> None:
    provider = RunPodProvider()
    capability = _capability(CostMode.PER_SECOND)
    provider_record = _provider_record(
        {"cost": {"kind": "per_vm_second", "rate_per_second": "0.003"}}
    )

    pricing = provider.pricing_model(capability, provider_record)

    assert isinstance(pricing, PerVmSecondPricing)
    assert pricing.rate_per_second == Decimal("0.003")


def test_runpod_pricing_model_preserves_legacy_gpu_hour_config() -> None:
    provider = RunPodProvider()
    capability = _capability(CostMode.PER_SECOND)

    pricing = provider.pricing_model(capability, _provider_record())

    assert isinstance(pricing, GpuHourPricing)
    assert pricing.per_second_active == Decimal("0.002")


@pytest.mark.anyio
async def test_provider_operations_round_trip_through_protocol() -> None:
    plugin: Provider = ExampleProvider()
    context = ProviderOperationContext(pool=object())
    credentials = ExampleCredentials(token="tok_123", account_id="acct_123")
    provider_record = _provider_record()
    capability = _capability()

    provisioned = await plugin.provision(
        ProvisionRequest(
            context=context,
            capability=capability,
            provider_record=provider_record,
            credentials=credentials,
            payload={"prompt": "hello"},
        )
    )
    status = await plugin.status(
        StatusRequest(
            context=context,
            provider_record=provider_record,
            credentials=credentials,
            external_id="vm-123",
        )
    )
    reconciled = await plugin.reconcile(
        ReconcileRequest(
            context=context,
            provider_record=provider_record,
            credentials=credentials,
            external_ids=("vm-123",),
        )
    )
    torn_down = await plugin.teardown(
        TeardownRequest(
            context=context,
            provider_record=provider_record,
            credentials=credentials,
            lease_id="lease-123",
            reason="test",
        )
    )

    assert provisioned.external_id == "vm-123"
    assert status.status is ResourceStatus.RUNNING
    assert reconciled.checked == 1
    assert torn_down.lease_id == "lease-123"


def test_runpod_credentials_accept_default_urls_and_hide_secret() -> None:
    credentials = RunPodCredentials(api_key="test-key")

    dumped = credentials.model_dump()

    assert credentials.api_key.get_secret_value() == "test-key"
    assert "test-key" not in repr(credentials)
    assert "test-key" not in str(dumped)


def test_registry_validates_mapping_inputs_without_mutating_them() -> None:
    registry = create_default_registry()
    raw: Mapping[str, object] = {"api_key": "test-key"}

    credentials = registry.validate_credentials("runpod", raw)

    assert isinstance(credentials, RunPodCredentials)
    assert raw == {"api_key": "test-key"}
