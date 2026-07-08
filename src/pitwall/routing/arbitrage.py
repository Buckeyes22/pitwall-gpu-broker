"""Price-latency arbitrage for provider/GPU options."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from pitwall.routing.carbon import (
    DEFAULT_CARBON_INTENSITY_SOURCE,
    CarbonIntensitySource,
    CarbonObjectiveWeights,
    score_carbon_objective,
)

_DEFAULT_COST_WEIGHT = Decimal("1")
_DEFAULT_LATENCY_WEIGHT = Decimal("0")
_DEFAULT_CARBON_WEIGHT = Decimal("0")


@dataclass(frozen=True, slots=True)
class ArbitrageOption:
    """One provider/GPU option with Decimal price, latency, and optional region."""

    provider_id: str
    gpu: str
    price: Decimal
    latency_ms: Decimal
    region: str | None = None


@dataclass(frozen=True, slots=True)
class ArbitrageScore:
    """Weighted score for one arbitrage option."""

    option: ArbitrageOption
    lambda_weight: Decimal
    objective: Decimal
    cost_component: Decimal
    latency_component: Decimal
    carbon_weight: Decimal = _DEFAULT_CARBON_WEIGHT
    carbon_intensity_gco2_per_kwh: Decimal = Decimal("0")
    carbon_component: Decimal = Decimal("0")
    cost_weight: Decimal = _DEFAULT_COST_WEIGHT

    @property
    def provider_id(self) -> str:
        """Provider id of the scored option."""

        return self.option.provider_id

    @property
    def gpu(self) -> str:
        """GPU name of the scored option."""

        return self.option.gpu

    def to_dict(self) -> dict[str, str]:
        """Return a deterministic, JSON-ready score representation."""

        return {
            "provider_id": self.option.provider_id,
            "gpu": self.option.gpu,
            "region": self.option.region or "",
            "price": str(self.option.price),
            "latency_ms": str(self.option.latency_ms),
            "cost_weight": str(self.cost_weight),
            "lambda_weight": str(self.lambda_weight),
            "carbon_weight": str(self.carbon_weight),
            "cost_component": str(self.cost_component),
            "latency_component": str(self.latency_component),
            "carbon_intensity_gco2_per_kwh": str(self.carbon_intensity_gco2_per_kwh),
            "carbon_component": str(self.carbon_component),
            "objective": str(self.objective),
        }


def score_arbitrage_option(
    option: ArbitrageOption,
    *,
    lambda_weight: Decimal,
    carbon_weight: Decimal = _DEFAULT_CARBON_WEIGHT,
    carbon_source: CarbonIntensitySource | None = None,
    cost_weight: Decimal = _DEFAULT_COST_WEIGHT,
) -> ArbitrageScore:
    """Return weighted cost + latency + carbon for one option."""

    provider_id = _non_empty_string(option.provider_id, "provider_id")
    gpu = _non_empty_string(option.gpu, "gpu")
    price = _non_negative_decimal(option.price, "price")
    latency_ms = _non_negative_decimal(option.latency_ms, "latency_ms")
    region = _optional_non_empty_string(option.region, "region")
    normalized_cost_weight = _non_negative_decimal(cost_weight, "cost_weight")
    normalized_latency_weight = _non_negative_decimal(lambda_weight, "lambda_weight")
    normalized_carbon_weight = _non_negative_decimal(carbon_weight, "carbon_weight")

    normalized_option = ArbitrageOption(
        provider_id=provider_id,
        gpu=gpu,
        price=price,
        latency_ms=latency_ms,
        region=region,
    )
    carbon_intensity = Decimal("0")
    if carbon_source is not None or normalized_carbon_weight > 0:
        active_source = carbon_source or DEFAULT_CARBON_INTENSITY_SOURCE
        carbon_intensity = _non_negative_decimal(
            active_source.intensity_for(provider_id=provider_id, region=region),
            "carbon intensity",
        )
    blended = score_carbon_objective(
        cost=price,
        latency_ms=latency_ms,
        carbon_intensity_gco2_per_kwh=carbon_intensity,
        weights=CarbonObjectiveWeights(
            cost_weight=normalized_cost_weight,
            latency_weight=normalized_latency_weight,
            carbon_weight=normalized_carbon_weight,
        ),
    )
    return ArbitrageScore(
        option=normalized_option,
        lambda_weight=normalized_latency_weight,
        objective=blended.objective,
        cost_component=blended.cost_component,
        latency_component=blended.latency_component,
        carbon_weight=normalized_carbon_weight,
        carbon_intensity_gco2_per_kwh=blended.carbon_intensity_gco2_per_kwh,
        carbon_component=blended.carbon_component,
        cost_weight=normalized_cost_weight,
    )


def sort_arbitrage_options(
    options: Iterable[ArbitrageOption],
    *,
    lambda_weight: Decimal,
    carbon_weight: Decimal = _DEFAULT_CARBON_WEIGHT,
    carbon_source: CarbonIntensitySource | None = None,
    cost_weight: Decimal = _DEFAULT_COST_WEIGHT,
) -> tuple[ArbitrageScore, ...]:
    """Return options sorted by objective with deterministic tie-breaks."""

    scores = tuple(
        score_arbitrage_option(
            option,
            lambda_weight=lambda_weight,
            carbon_weight=carbon_weight,
            carbon_source=carbon_source,
            cost_weight=cost_weight,
        )
        for option in options
    )
    return tuple(sorted(scores, key=_score_sort_key))


def select_arbitrage_option(
    options: Iterable[ArbitrageOption],
    *,
    lambda_weight: Decimal,
    carbon_weight: Decimal = _DEFAULT_CARBON_WEIGHT,
    carbon_source: CarbonIntensitySource | None = None,
    cost_weight: Decimal = _DEFAULT_COST_WEIGHT,
) -> ArbitrageScore:
    """Select the option minimizing weighted cost + latency + carbon."""

    ranked = sort_arbitrage_options(
        options,
        lambda_weight=lambda_weight,
        carbon_weight=carbon_weight,
        carbon_source=carbon_source,
        cost_weight=cost_weight,
    )
    if not ranked:
        raise ValueError("options must contain at least one option")
    return ranked[0]


def _score_sort_key(
    score: ArbitrageScore,
) -> tuple[Decimal, Decimal, Decimal, Decimal, str, str]:
    return (
        score.objective,
        score.cost_component,
        score.option.latency_ms,
        score.carbon_component,
        score.option.provider_id,
        score.option.gpu,
    )


def _non_empty_string(value: object, name: str) -> str:
    normalized = _optional_non_empty_string(value, name)
    if normalized is None:
        raise ValueError(f"{name} must be a non-empty string")
    return normalized


def _optional_non_empty_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    stripped = value.strip()
    if not stripped:
        return None
    if value != stripped:
        raise ValueError(f"{name} must not include surrounding whitespace")
    return stripped


def _non_negative_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


__all__ = [
    "ArbitrageOption",
    "ArbitrageScore",
    "score_arbitrage_option",
    "select_arbitrage_option",
    "sort_arbitrage_options",
]
