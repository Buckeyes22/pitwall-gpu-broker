"""Hermetic tests for provider drift detection.

Covers the four comparison dimensions (enabled, health_status, price,
availability) plus snapshot helpers and a determinism property.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability
from pitwall.core.models import Provider as ProviderRecord
from pitwall.providers.drift import (
    DriftSeverity,
    ProviderObservedState,
    detect_drift,
    observe_from_runpod_snapshot,
    observe_from_status_result,
)
from pitwall.providers.interface import ResourceStatus
from pitwall.runpod_client.discovery import (
    DatacenterCatalogEntry,
    GpuCatalogEntry,
    GpuDiscoverySnapshot,
)

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _capability(*, cost_mode: CostMode = CostMode.PER_SECOND) -> Capability:
    return Capability(
        id="cap_test",
        name="test-cap",
        version="1",
        class_=CapabilityClass.GPU_LEASE,
        cost_mode=cost_mode,
        source=CapabilitySource.API,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider(
    *,
    enabled: bool = True,
    health_status: str = "healthy",
    config: dict[str, Any] | None = None,
    provider_type: ProviderType = ProviderType.POD_LEASE,
    region: str | None = None,
    cloud_type: str | None = None,
) -> ProviderRecord:
    return ProviderRecord(
        id="prov_test",
        capability_id="cap_test",
        name="test-provider",
        provider_type=provider_type,
        enabled=enabled,
        health_status=health_status,
        config=config or {},
        region=region,
        cloud_type=cloud_type,
        priority=0,
        source=CapabilitySource.API,
        updated_at=_NOW,
    )


def _observed(
    *,
    status: ResourceStatus | None = None,
    price_per_second: Decimal | None = None,
    availability: bool | None = None,
) -> ProviderObservedState:
    return ProviderObservedState(
        provider_id="prov_test",
        status=status,
        price_per_second=price_per_second,
        availability=availability,
    )


# ---------------------------------------------------------------------------
# Identity guard
# ---------------------------------------------------------------------------


def test_detect_drift_returns_critical_when_provider_id_mismatches() -> None:
    provider = _provider()
    observed = ProviderObservedState(provider_id="wrong_id")
    findings = detect_drift(provider, observed)
    assert len(findings) == 1
    assert findings[0].field == "provider_id"
    assert findings[0].severity == DriftSeverity.CRITICAL


# ---------------------------------------------------------------------------
# Enabled dimension
# ---------------------------------------------------------------------------


def test_no_drift_when_enabled_and_running() -> None:
    provider = _provider(enabled=True)
    observed = _observed(status=ResourceStatus.RUNNING)
    assert detect_drift(provider, observed) == []


def test_drift_disabled_but_running() -> None:
    provider = _provider(enabled=False)
    observed = _observed(status=ResourceStatus.RUNNING)
    findings = detect_drift(provider, observed)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.field == "enabled"
    assert finding.severity == DriftSeverity.HIGH
    assert finding.expected is False
    assert finding.observed is True


def test_drift_enabled_but_terminated() -> None:
    provider = _provider(enabled=True, health_status="unhealthy")
    observed = _observed(status=ResourceStatus.TERMINATED)
    findings = detect_drift(provider, observed)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.field == "enabled"
    assert finding.severity == DriftSeverity.MEDIUM
    assert finding.expected is True
    assert finding.observed is False


# ---------------------------------------------------------------------------
# Health status dimension
# ---------------------------------------------------------------------------


def test_no_drift_when_health_status_matches() -> None:
    provider = _provider(health_status="healthy")
    observed = _observed(status=ResourceStatus.RUNNING)
    assert detect_drift(provider, observed) == []


@pytest.mark.parametrize(
    ("health_status", "status", "expected_severity"),
    [
        ("healthy", ResourceStatus.FAILED, DriftSeverity.HIGH),
        ("unknown", ResourceStatus.RUNNING, DriftSeverity.HIGH),
        ("unhealthy", ResourceStatus.RUNNING, DriftSeverity.HIGH),
    ],
)
def test_drift_health_status_mismatch(
    health_status: str,
    status: ResourceStatus,
    expected_severity: DriftSeverity,
) -> None:
    provider = _provider(health_status=health_status)
    observed = _observed(status=status)
    findings = detect_drift(provider, observed)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.field == "health_status"
    assert finding.severity == expected_severity
    assert finding.observed == status.value


def test_no_health_drift_when_observed_status_is_none() -> None:
    provider = _provider(health_status="healthy")
    observed = _observed(status=None)
    assert detect_drift(provider, observed) == []


# ---------------------------------------------------------------------------
# Price dimension
# ---------------------------------------------------------------------------


def test_no_drift_when_price_matches() -> None:
    provider = _provider(config={"cost": {"per_second_active": "0.001"}})
    observed = _observed(price_per_second=Decimal("0.001"))
    cap = _capability()
    assert detect_drift(provider, observed, capability=cap) == []


def test_drift_when_price_differs() -> None:
    provider = _provider(config={"cost": {"per_second_active": "0.001"}})
    observed = _observed(price_per_second=Decimal("0.002"))
    cap = _capability()
    findings = detect_drift(provider, observed, capability=cap)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.field == "price_per_second"
    assert finding.severity == DriftSeverity.MEDIUM
    assert finding.expected == Decimal("0.001")
    assert finding.observed == Decimal("0.002")


def test_no_price_drift_for_non_terminating_hourly_price_with_rounded_config() -> None:
    gpu = GpuCatalogEntry(
        gpu_type_id="NVIDIA L4",
        secure_price=Decimal("0.44"),
        datacenter_ids=("US-NY-1",),
    )
    dc = DatacenterCatalogEntry(
        datacenter_id="US-NY-1",
        gpu_types=("NVIDIA L4",),
        gpu_availability={"NVIDIA L4": True},
    )
    snapshot = GpuDiscoverySnapshot(
        fetched_at=_NOW,
        gpus=(gpu,),
        datacenters=(dc,),
    )
    provider = _provider(
        config={
            "gpu_type_id": "NVIDIA L4",
            "cloud_type": "SECURE",
            "cost": {"per_second_active": "0.000122"},
        },
        region="US-NY-1",
    )
    observed = observe_from_runpod_snapshot(provider, snapshot)
    cap = _capability()

    assert observed.price_per_second == Decimal("0.44") / Decimal(3600)
    assert detect_drift(provider, observed, capability=cap) == []


def test_skip_price_when_capability_missing() -> None:
    provider = _provider(config={"cost": {"per_second_active": "0.001"}})
    observed = _observed(price_per_second=Decimal("0.002"))
    assert detect_drift(provider, observed) == []


def test_skip_price_when_observed_price_missing() -> None:
    provider = _provider(config={"cost": {"per_second_active": "0.001"}})
    observed = _observed(price_per_second=None)
    cap = _capability()
    assert detect_drift(provider, observed, capability=cap) == []


def test_skip_price_for_unsupported_pricing_model() -> None:
    provider = _provider(
        config={
            "cost": {
                "kind": "per_token",
                "per_million_input_tokens": "0.30",
                "per_million_output_tokens": "0.60",
            }
        }
    )
    observed = _observed(price_per_second=Decimal("0.001"))
    cap = _capability(cost_mode=CostMode.PER_TOKEN)
    assert detect_drift(provider, observed, capability=cap) == []


# ---------------------------------------------------------------------------
# Availability dimension
# ---------------------------------------------------------------------------


def test_no_drift_when_availability_matches() -> None:
    provider = _provider(enabled=True)
    observed = _observed(availability=True)
    assert detect_drift(provider, observed) == []


def test_drift_enabled_but_unavailable() -> None:
    provider = _provider(enabled=True)
    observed = _observed(availability=False)
    findings = detect_drift(provider, observed)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.field == "availability"
    assert finding.severity == DriftSeverity.HIGH
    assert finding.expected is True
    assert finding.observed is False


def test_drift_disabled_but_available() -> None:
    provider = _provider(enabled=False)
    observed = _observed(availability=True)
    findings = detect_drift(provider, observed)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.field == "availability"
    assert finding.severity == DriftSeverity.LOW
    assert finding.expected is False
    assert finding.observed is True


def test_skip_availability_when_observed_availability_missing() -> None:
    provider = _provider(enabled=True)
    observed = _observed(availability=None)
    assert detect_drift(provider, observed) == []


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def test_observe_from_status_result() -> None:
    observed = observe_from_status_result("prov_test", ResourceStatus.RUNNING)
    assert observed.provider_id == "prov_test"
    assert observed.status == ResourceStatus.RUNNING
    assert observed.price_per_second is None
    assert observed.availability is None


def test_observe_from_runpod_snapshot_price_and_availability() -> None:
    gpu = GpuCatalogEntry(
        gpu_type_id="NVIDIA L4",
        secure_price=Decimal("3.60"),
        datacenter_ids=("US-NY-1",),
    )
    dc = DatacenterCatalogEntry(
        datacenter_id="US-NY-1",
        gpu_types=("NVIDIA L4",),
        gpu_availability={"NVIDIA L4": True},
    )
    snapshot = GpuDiscoverySnapshot(
        fetched_at=_NOW,
        gpus=(gpu,),
        datacenters=(dc,),
    )
    provider = _provider(
        config={"gpu_type_id": "NVIDIA L4", "cloud_type": "SECURE"},
        region="US-NY-1",
    )
    observed = observe_from_runpod_snapshot(provider, snapshot)
    assert observed.provider_id == "prov_test"
    assert observed.price_per_second == Decimal("0.001")  # 3.60 / 3600
    assert observed.availability is True


def test_observe_from_runpod_snapshot_unavailable_when_dc_missing() -> None:
    gpu = GpuCatalogEntry(
        gpu_type_id="NVIDIA L4",
        secure_price=Decimal("3.60"),
        datacenter_ids=("US-NY-1",),
    )
    dc = DatacenterCatalogEntry(
        datacenter_id="US-NY-1",
        gpu_types=("NVIDIA L4",),
        gpu_availability={"NVIDIA L4": False},
    )
    snapshot = GpuDiscoverySnapshot(
        fetched_at=_NOW,
        gpus=(gpu,),
        datacenters=(dc,),
    )
    provider = _provider(
        config={"gpu_type_id": "NVIDIA L4"},
        region="US-NY-1",
    )
    observed = observe_from_runpod_snapshot(provider, snapshot)
    assert observed.availability is False


def test_observe_from_runpod_snapshot_honors_data_center_id_alias() -> None:
    gpu = GpuCatalogEntry(
        gpu_type_id="NVIDIA L4",
        secure_price=Decimal("3.60"),
        datacenter_ids=("US-NY-1", "US-TX-1"),
    )
    unavailable_dc = DatacenterCatalogEntry(
        datacenter_id="US-NY-1",
        gpu_types=("NVIDIA L4",),
        gpu_availability={"NVIDIA L4": False},
    )
    available_dc = DatacenterCatalogEntry(
        datacenter_id="US-TX-1",
        gpu_types=("NVIDIA L4",),
        gpu_availability={"NVIDIA L4": True},
    )
    snapshot = GpuDiscoverySnapshot(
        fetched_at=_NOW,
        gpus=(gpu,),
        datacenters=(unavailable_dc, available_dc),
    )
    provider = _provider(
        config={
            "gpu_type_id": "NVIDIA L4",
            "data_center_id": "US-NY-1",
        },
    )

    observed = observe_from_runpod_snapshot(provider, snapshot)

    assert observed.availability is False
    assert observed.raw["datacenter_ids"] == ("US-NY-1",)


def test_observe_from_runpod_snapshot_honors_data_center_ids_camel_alias() -> None:
    gpu = GpuCatalogEntry(
        gpu_type_id="NVIDIA L4",
        secure_price=Decimal("3.60"),
        datacenter_ids=("US-NY-1", "US-TX-1"),
    )
    unavailable_dc = DatacenterCatalogEntry(
        datacenter_id="US-NY-1",
        gpu_types=("NVIDIA L4",),
        gpu_availability={"NVIDIA L4": False},
    )
    available_dc = DatacenterCatalogEntry(
        datacenter_id="US-TX-1",
        gpu_types=("NVIDIA L4",),
        gpu_availability={"NVIDIA L4": True},
    )
    snapshot = GpuDiscoverySnapshot(
        fetched_at=_NOW,
        gpus=(gpu,),
        datacenters=(unavailable_dc, available_dc),
    )
    provider = _provider(
        config={
            "gpu_type_id": "NVIDIA L4",
            "dataCenterIds": ["US-NY-1"],
        },
    )

    observed = observe_from_runpod_snapshot(provider, snapshot)

    assert observed.availability is False
    assert observed.raw["datacenter_ids"] == ("US-NY-1",)


# ---------------------------------------------------------------------------
# Determinism property
# ---------------------------------------------------------------------------


@given(
    enabled=st.booleans(),
    health_status=st.sampled_from(["healthy", "unhealthy", "unknown"]),
    observed_status=st.sampled_from(list(ResourceStatus)),
    observed_price=st.one_of(
        st.none(),
        st.decimals(
            allow_nan=False,
            allow_infinity=False,
            places=6,
            min_value=0,
            max_value=1000,
        ),
    ),
    observed_availability=st.one_of(st.none(), st.booleans()),
)
def test_detect_drift_is_deterministic(
    enabled: bool,
    health_status: str,
    observed_status: ResourceStatus,
    observed_price: Decimal | None,
    observed_availability: bool | None,
) -> None:
    provider = _provider(enabled=enabled, health_status=health_status)
    observed = ProviderObservedState(
        provider_id=provider.id,
        status=observed_status,
        price_per_second=observed_price,
        availability=observed_availability,
    )
    cap = _capability()
    result1 = detect_drift(provider, observed, capability=cap)
    result2 = detect_drift(provider, observed, capability=cap)
    assert result1 == result2
