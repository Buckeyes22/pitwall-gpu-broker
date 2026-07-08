"""Provider drift detection — compare expected vs observed provider state.

The detector is deterministic and read-only given snapshots.  It returns
structured :class:`DriftFinding` objects for every field where the live
observed state diverges from the persisted ``Provider`` record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from pitwall.core.models import Capability
from pitwall.core.models import Provider as ProviderRecord
from pitwall.cost.estimator import (
    GpuHourPricing,
    PerSecondPricing,
    PerVmSecondPricing,
    TaggedPricingModel,
    parse_pricing_model,
)
from pitwall.providers.interface import ResourceStatus
from pitwall.runpod_client.discovery import (
    DatacenterCatalogEntry,
    GpuCatalogEntry,
    GpuDiscoverySnapshot,
)

_HOUR_SECONDS = Decimal(3600)
_PRICE_PER_SECOND_QUANTUM = Decimal("0.000001")


class DriftSeverity(StrEnum):
    """Severity of a drift finding."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class DriftFinding:
    """Structured drift finding for a single provider field."""

    provider_id: str
    field: str
    expected: Any
    observed: Any
    severity: DriftSeverity
    message: str = ""


@dataclass(frozen=True, slots=True)
class ProviderObservedState:
    """Normalized observed provider state extracted from a live snapshot."""

    provider_id: str
    status: ResourceStatus | None = None
    price_per_second: Decimal | None = None
    availability: bool | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


def detect_drift(
    provider: ProviderRecord,
    observed: ProviderObservedState,
    *,
    capability: Capability | None = None,
) -> list[DriftFinding]:
    """Compare *provider* (expected) against *observed* and return findings.

    The comparison covers four dimensions:

    1. **enabled** — a disabled provider with a running resource, or an
       enabled provider whose resource is terminated.
    2. **health_status** — the persisted health string vs the observed
       :class:`ResourceStatus` mapped to ``healthy`` / ``unknown`` /
       ``unhealthy``.
    3. **price_per_second** — the configured time-based rate vs the live
       rate (only when *capability* is supplied and the pricing model is
       ``GpuHourPricing``, ``PerSecondPricing``, or ``PerVmSecondPricing``).
    4. **availability** — an enabled provider that is live-unavailable, or a
       disabled provider that is live-available.

    Results are returned in a deterministic order.
    """
    if observed.provider_id != provider.id:
        return [
            DriftFinding(
                provider_id=provider.id,
                field="provider_id",
                expected=provider.id,
                observed=observed.provider_id,
                severity=DriftSeverity.CRITICAL,
                message="Observed provider id does not match expected",
            )
        ]

    findings: list[DriftFinding] = []
    findings.extend(_compare_enabled(provider, observed))
    findings.extend(_compare_health_status(provider, observed))
    findings.extend(_compare_price(provider, observed, capability))
    findings.extend(_compare_availability(provider, observed))
    return findings


def _compare_enabled(
    provider: ProviderRecord,
    observed: ProviderObservedState,
) -> list[DriftFinding]:
    findings: list[DriftFinding] = []
    if not provider.enabled and observed.status == ResourceStatus.RUNNING:
        findings.append(
            DriftFinding(
                provider_id=provider.id,
                field="enabled",
                expected=provider.enabled,
                observed=True,
                severity=DriftSeverity.HIGH,
                message="Provider is disabled but a running resource was observed",
            )
        )
    if provider.enabled and observed.status == ResourceStatus.TERMINATED:
        findings.append(
            DriftFinding(
                provider_id=provider.id,
                field="enabled",
                expected=provider.enabled,
                observed=False,
                severity=DriftSeverity.MEDIUM,
                message="Provider is enabled but observed resource is terminated",
            )
        )
    return findings


def _compare_health_status(
    provider: ProviderRecord,
    observed: ProviderObservedState,
) -> list[DriftFinding]:
    if observed.status is None:
        return []
    expected = provider.health_status.lower().strip()
    observed_health = _status_to_health(observed.status)
    if expected == observed_health:
        return []
    return [
        DriftFinding(
            provider_id=provider.id,
            field="health_status",
            expected=provider.health_status,
            observed=observed.status.value,
            severity=DriftSeverity.HIGH,
            message=(
                f"Expected health {provider.health_status!r}, observed {observed.status.value!r}"
            ),
        )
    ]


def _status_to_health(status: ResourceStatus) -> str:
    if status == ResourceStatus.RUNNING:
        return "healthy"
    if status in (ResourceStatus.PROVISIONING, ResourceStatus.UNKNOWN):
        return "unknown"
    return "unhealthy"


def _compare_price(
    provider: ProviderRecord,
    observed: ProviderObservedState,
    capability: Capability | None,
) -> list[DriftFinding]:
    if observed.price_per_second is None:
        return []
    try:
        expected_pricing = parse_pricing_model(
            provider,
            cost_mode=capability.cost_mode if capability is not None else None,
        )
    except ValueError:
        return []
    expected_rate = _extract_rate_per_second(expected_pricing)
    if expected_rate is None:
        return []
    if _price_rates_match(expected_rate, observed.price_per_second):
        return []
    return [
        DriftFinding(
            provider_id=provider.id,
            field="price_per_second",
            expected=expected_rate,
            observed=observed.price_per_second,
            severity=DriftSeverity.MEDIUM,
            message="Observed price differs from configured price",
        )
    ]


def _extract_rate_per_second(pricing: TaggedPricingModel) -> Decimal | None:
    if isinstance(pricing, GpuHourPricing):
        return pricing.per_second_active
    if isinstance(pricing, (PerSecondPricing, PerVmSecondPricing)):
        return pricing.rate_per_second
    return None


def _price_rates_match(expected: Decimal, observed: Decimal) -> bool:
    return expected.quantize(
        _PRICE_PER_SECOND_QUANTUM,
        rounding=ROUND_HALF_UP,
    ) == observed.quantize(
        _PRICE_PER_SECOND_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _compare_availability(
    provider: ProviderRecord,
    observed: ProviderObservedState,
) -> list[DriftFinding]:
    if observed.availability is None:
        return []
    if provider.enabled and not observed.availability:
        return [
            DriftFinding(
                provider_id=provider.id,
                field="availability",
                expected=True,
                observed=False,
                severity=DriftSeverity.HIGH,
                message="Provider is enabled but observed as unavailable",
            )
        ]
    if not provider.enabled and observed.availability:
        return [
            DriftFinding(
                provider_id=provider.id,
                field="availability",
                expected=False,
                observed=True,
                severity=DriftSeverity.LOW,
                message="Provider is disabled but observed as available",
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def observe_from_status_result(
    provider_id: str,
    status: ResourceStatus,
    *,
    raw: Mapping[str, Any] | None = None,
) -> ProviderObservedState:
    """Build a :class:`ProviderObservedState` from a single status read."""
    return ProviderObservedState(
        provider_id=provider_id,
        status=status,
        raw=raw or {},
    )


def observe_from_runpod_snapshot(
    provider: ProviderRecord,
    snapshot: GpuDiscoverySnapshot,
    *,
    gpu_type_id: str | None = None,
    datacenter_id: str | None = None,
    cloud_type: str | None = None,
) -> ProviderObservedState:
    """Build observed state for a RunPod provider from a GPU discovery snapshot.

    Price is normalised to **per-second** by dividing the live hourly rate by
    3600.  Availability is ``True`` when the configured GPU type is reported
    as available in the configured datacenter (or in *any* datacenter when no
    datacenter is pinned).
    """
    config = provider.config
    _gpu_type_id = gpu_type_id or _config_str(config, "gpu_type_id", "gpu_type")
    _dc_ids = _runpod_datacenter_ids(provider, explicit_datacenter_id=datacenter_id)
    _cloud = (
        cloud_type or provider.cloud_type or _config_str(config, "cloud_type") or "SECURE"
    ).upper()

    gpu = snapshot.gpu_by_id(_gpu_type_id) if _gpu_type_id else None
    price_per_second: Decimal | None = None
    availability = False

    if gpu is not None:
        price_per_second = _runpod_price_per_second(gpu, _cloud)
        availability = _runpod_availability(gpu, _dc_ids, snapshot.datacenters)

    return ProviderObservedState(
        provider_id=provider.id,
        price_per_second=price_per_second,
        availability=availability,
        raw={
            "gpu_type_id": _gpu_type_id,
            "datacenter_id": _dc_ids[0] if len(_dc_ids) == 1 else None,
            "datacenter_ids": _dc_ids,
            "cloud_type": _cloud,
            "snapshot_fetched_at": snapshot.fetched_at.isoformat(),
        },
    )


def _runpod_price_per_second(gpu: GpuCatalogEntry, cloud_type: str) -> Decimal | None:
    price: Decimal | None = None
    if cloud_type == "SECURE":
        price = gpu.secure_price
    elif cloud_type == "COMMUNITY":
        price = gpu.community_price
    elif cloud_type == "SECURE_SPOT":
        price = gpu.secure_spot_price
    elif cloud_type == "COMMUNITY_SPOT":
        price = gpu.community_spot_price
    else:
        price = gpu.secure_price
    if price is None:
        return None
    return price / _HOUR_SECONDS


def _runpod_availability(
    gpu: GpuCatalogEntry,
    datacenter_ids: tuple[str, ...],
    datacenters: tuple[DatacenterCatalogEntry, ...],
) -> bool:
    if datacenter_ids:
        configured = set(datacenter_ids)
        for dc in datacenters:
            if dc.datacenter_id in configured and dc.gpu_availability.get(
                gpu.gpu_type_id,
                False,
            ):
                return True
        return False
    return any(
        dc.gpu_availability.get(gpu.gpu_type_id, False)
        for dc in datacenters
        if gpu.gpu_type_id in dc.gpu_types
    )


def _config_str(config: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _runpod_datacenter_ids(
    provider: ProviderRecord,
    *,
    explicit_datacenter_id: str | None,
) -> tuple[str, ...]:
    explicit = _non_empty_str(explicit_datacenter_id)
    if explicit is not None:
        return (explicit,)

    config = provider.config
    configured = _config_str(
        config,
        "data_center_id",
        "dataCenterId",
        "datacenter_id",
        "dc_id",
    )
    if configured is not None:
        return (configured,)

    configured_list = _config_str_list(
        config,
        "data_center_ids",
        "dataCenterIds",
        "datacenter_ids",
    )
    if configured_list:
        return tuple(configured_list)

    region = _non_empty_str(provider.region)
    return (region,) if region is not None else ()


def _config_str_list(config: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
    for key in keys:
        value = config.get(key)
        if isinstance(value, str):
            items = tuple(item.strip() for item in value.split(",") if item.strip())
            if items:
                return items
        if isinstance(value, list | tuple):
            items = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
            if items:
                return items
    return ()


def _non_empty_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


__all__ = [
    "DriftFinding",
    "DriftSeverity",
    "ProviderObservedState",
    "detect_drift",
    "observe_from_runpod_snapshot",
    "observe_from_status_result",
]
