"""Carbon-intensity primitives for deterministic routing decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

DEFAULT_UNKNOWN_CARBON_INTENSITY_GCO2_PER_KWH = Decimal("500")

DEFAULT_REGION_CARBON_INTENSITIES_GCO2_PER_KWH: Mapping[str, Decimal] = MappingProxyType(
    {
        "CA-MTL-1": Decimal("60"),
        "EU-CZ-1": Decimal("390"),
        "EU-RO-1": Decimal("250"),
        "EU-SE-1": Decimal("45"),
        "US-CA-1": Decimal("220"),
        "US-CA-2": Decimal("220"),
        "US-KS-2": Decimal("455"),
        "US-NY-1": Decimal("300"),
    }
)

_MISSING = object()
_ProviderLike = Mapping[str, Any] | object


@dataclass(frozen=True, slots=True)
class CarbonObjectiveWeights:
    """Weights used to blend cost, latency, and carbon intensity."""

    cost_weight: Decimal = Decimal("1")
    latency_weight: Decimal = Decimal("0")
    carbon_weight: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cost_weight",
            _non_negative_decimal(self.cost_weight, "cost_weight"),
        )
        object.__setattr__(
            self,
            "latency_weight",
            _non_negative_decimal(self.latency_weight, "latency_weight"),
        )
        object.__setattr__(
            self,
            "carbon_weight",
            _non_negative_decimal(self.carbon_weight, "carbon_weight"),
        )


@dataclass(frozen=True, slots=True)
class CarbonObjectiveScore:
    """Weighted cost + latency + carbon objective and component breakdown."""

    weights: CarbonObjectiveWeights
    objective: Decimal
    cost_component: Decimal
    latency_component: Decimal
    carbon_component: Decimal
    carbon_intensity_gco2_per_kwh: Decimal

    def to_dict(self) -> dict[str, str]:
        """Return a deterministic, JSON-ready score representation."""

        return {
            "cost_weight": str(self.weights.cost_weight),
            "latency_weight": str(self.weights.latency_weight),
            "carbon_weight": str(self.weights.carbon_weight),
            "cost_component": str(self.cost_component),
            "latency_component": str(self.latency_component),
            "carbon_component": str(self.carbon_component),
            "carbon_intensity_gco2_per_kwh": str(self.carbon_intensity_gco2_per_kwh),
            "objective": str(self.objective),
        }


@runtime_checkable
class CarbonIntensitySource(Protocol):
    """Provider/region carbon-intensity lookup seam."""

    def intensity_for(self, *, provider_id: str, region: str | None) -> Decimal:
        """Return gCO2e/kWh for a provider region."""
        ...


@dataclass(frozen=True, slots=True)
class StaticCarbonIntensitySource:
    """Static provider/region carbon-intensity table with deterministic fallback."""

    provider_region_intensities: Mapping[tuple[str, str], Decimal] = field(default_factory=dict)
    region_intensities: Mapping[str, Decimal] = field(
        default_factory=lambda: DEFAULT_REGION_CARBON_INTENSITIES_GCO2_PER_KWH
    )
    default_intensity: Decimal = DEFAULT_UNKNOWN_CARBON_INTENSITY_GCO2_PER_KWH

    def __post_init__(self) -> None:
        provider_region = {
            (
                _non_empty_string(provider_id, "provider_id"),
                _non_empty_string(region, "region"),
            ): _non_negative_decimal(intensity, "carbon intensity")
            for (provider_id, region), intensity in self.provider_region_intensities.items()
        }
        region = {
            _non_empty_string(region_id, "region"): _non_negative_decimal(
                intensity,
                "carbon intensity",
            )
            for region_id, intensity in self.region_intensities.items()
        }
        object.__setattr__(self, "provider_region_intensities", MappingProxyType(provider_region))
        object.__setattr__(self, "region_intensities", MappingProxyType(region))
        object.__setattr__(
            self,
            "default_intensity",
            _non_negative_decimal(self.default_intensity, "carbon intensity"),
        )

    def intensity_for(self, *, provider_id: str, region: str | None) -> Decimal:
        """Return provider-specific, region-specific, or fallback intensity."""

        normalized_provider_id = _non_empty_string(provider_id, "provider_id")
        normalized_region = _optional_non_empty_string(region, "region")
        if normalized_region is not None:
            provider_region = (normalized_provider_id, normalized_region)
            if provider_region in self.provider_region_intensities:
                return self.provider_region_intensities[provider_region]
            if normalized_region in self.region_intensities:
                return self.region_intensities[normalized_region]
        return self.default_intensity


def score_carbon_objective(
    *,
    cost: Decimal,
    latency_ms: Decimal,
    carbon_intensity_gco2_per_kwh: Decimal,
    weights: CarbonObjectiveWeights,
) -> CarbonObjectiveScore:
    """Blend cost, latency, and carbon into one deterministic objective."""

    normalized_cost = _non_negative_usd_decimal(cost, "cost")
    normalized_latency_ms = _non_negative_decimal(latency_ms, "latency_ms")
    normalized_carbon = _non_negative_decimal(
        carbon_intensity_gco2_per_kwh,
        "carbon intensity",
    )
    cost_component = weights.cost_weight * normalized_cost
    latency_component = weights.latency_weight * normalized_latency_ms
    carbon_component = weights.carbon_weight * normalized_carbon
    objective = cost_component + latency_component + carbon_component
    return CarbonObjectiveScore(
        weights=weights,
        objective=objective,
        cost_component=cost_component,
        latency_component=latency_component,
        carbon_component=carbon_component,
        carbon_intensity_gco2_per_kwh=normalized_carbon,
    )


def carbon_intensity_for_provider(
    provider: _ProviderLike,
    *,
    source: CarbonIntensitySource | None = None,
) -> Decimal:
    """Return carbon intensity for a provider-shaped object or mapping."""

    active_source = source or DEFAULT_CARBON_INTENSITY_SOURCE
    return active_source.intensity_for(
        provider_id=_provider_id(provider),
        region=_provider_region(provider),
    )


def _provider_id(provider: _ProviderLike) -> str:
    return _non_empty_string(_field(provider, "id"), "provider_id")


def _provider_region(provider: _ProviderLike) -> str | None:
    return _first_optional_string(
        _field(provider, "region"),
        _field(provider, "datacenter"),
        _field(provider, "dataCenterId"),
        _field(provider, "data_center_id"),
        _field(provider, "datacenter_id"),
        _field(provider, "dc_id"),
        _config_first_string(provider, "dataCenterIds"),
        _config_first_string(provider, "data_center_ids"),
        _config_value(provider, "dataCenterId"),
        _config_value(provider, "data_center_id"),
        _config_value(provider, "datacenter"),
        _config_value(provider, "datacenter_id"),
        _config_value(provider, "dc_id"),
    )


def _config_first_string(provider: _ProviderLike, key: str) -> object:
    value = _config_value(provider, key)
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        for item in value:
            text = _optional_non_empty_string(item, key)
            if text is not None:
                return text
    return _MISSING


def _first_optional_string(*values: object) -> str | None:
    for value in values:
        text = _optional_non_empty_string(value, "region")
        if text is not None:
            return text
    return None


def _field(provider: _ProviderLike, key: str) -> object:
    if isinstance(provider, Mapping):
        return provider.get(key, _MISSING)
    return getattr(provider, key, _MISSING)


def _config(provider: _ProviderLike) -> Mapping[str, Any]:
    raw = _field(provider, "config")
    if isinstance(raw, Mapping):
        return raw
    return {}


def _config_value(provider: _ProviderLike, key: str) -> object:
    config = _config(provider)
    if key in config:
        return config[key]
    return _MISSING


def _optional_non_empty_string(value: object, name: str) -> str | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    stripped = value.strip()
    if not stripped:
        return None
    if value != stripped:
        raise ValueError(f"{name} must not include surrounding whitespace")
    return stripped


def _non_empty_string(value: object, name: str) -> str:
    text = _optional_non_empty_string(value, name)
    if text is None:
        raise ValueError(f"{name} must be a non-empty string")
    return text


def _non_negative_usd_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _non_negative_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a Decimal")
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name} must be a Decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


DEFAULT_CARBON_INTENSITY_SOURCE = StaticCarbonIntensitySource()


__all__ = [
    "CarbonIntensitySource",
    "CarbonObjectiveScore",
    "CarbonObjectiveWeights",
    "DEFAULT_CARBON_INTENSITY_SOURCE",
    "DEFAULT_REGION_CARBON_INTENSITIES_GCO2_PER_KWH",
    "DEFAULT_UNKNOWN_CARBON_INTENSITY_GCO2_PER_KWH",
    "StaticCarbonIntensitySource",
    "carbon_intensity_for_provider",
    "score_carbon_objective",
]
