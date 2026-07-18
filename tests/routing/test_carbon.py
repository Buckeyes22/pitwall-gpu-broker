"""Hermetic tests for carbon-aware routing primitives."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pitwall.routing.carbon import (
    CarbonObjectiveWeights,
    StaticCarbonIntensitySource,
    carbon_intensity_for_provider,
    score_carbon_objective,
)


def test_static_source_prefers_provider_region_override() -> None:
    source = StaticCarbonIntensitySource(
        provider_region_intensities={
            ("runpod", "US-KS-2"): Decimal("455"),
        },
        region_intensities={"US-KS-2": Decimal("510")},
        default_intensity=Decimal("700"),
    )

    assert source.intensity_for(provider_id="runpod", region="US-KS-2") == Decimal("455")


def test_static_source_uses_region_default_when_provider_override_missing() -> None:
    source = StaticCarbonIntensitySource(
        provider_region_intensities={
            ("runpod", "US-KS-2"): Decimal("455"),
        },
        region_intensities={"EU-SE-1": Decimal("45")},
        default_intensity=Decimal("700"),
    )

    assert source.intensity_for(provider_id="lambda", region="EU-SE-1") == Decimal("45")


def test_static_source_returns_default_for_unknown_region() -> None:
    source = StaticCarbonIntensitySource(default_intensity=Decimal("700"))

    assert source.intensity_for(provider_id="unknown-provider", region="unknown-region") == Decimal(
        "700"
    )


def test_static_source_rejects_invalid_numeric_values() -> None:
    with pytest.raises(ValueError, match="carbon intensity must be non-negative"):
        StaticCarbonIntensitySource(
            region_intensities={"US-KS-2": Decimal("-1")},
        )


def test_provider_carbon_lookup_reads_mapping_region() -> None:
    source = StaticCarbonIntensitySource(
        region_intensities={"EU-SE-1": Decimal("45")},
        default_intensity=Decimal("700"),
    )

    intensity = carbon_intensity_for_provider(
        {
            "id": "runpod-eu",
            "region": "EU-SE-1",
        },
        source=source,
    )

    assert intensity == Decimal("45")


def test_provider_carbon_lookup_reads_config_datacenter_ids_when_region_missing() -> None:
    source = StaticCarbonIntensitySource(
        region_intensities={"US-CA-1": Decimal("220")},
        default_intensity=Decimal("700"),
    )

    intensity = carbon_intensity_for_provider(
        {
            "id": "runpod-ca",
            "config": {"dataCenterIds": ["US-CA-1", "US-KS-2"]},
        },
        source=source,
    )

    assert intensity == Decimal("220")


def test_score_carbon_objective_blends_cost_latency_and_carbon_weights() -> None:
    score = score_carbon_objective(
        cost=Decimal("0.120000"),
        latency_ms=Decimal("250"),
        carbon_intensity_gco2_per_kwh=Decimal("45"),
        weights=CarbonObjectiveWeights(
            cost_weight=Decimal("1"),
            latency_weight=Decimal("0.0002"),
            carbon_weight=Decimal("0.0001"),
        ),
    )

    assert score.cost_component == Decimal("0.120000")
    assert score.latency_component == Decimal("0.0500")
    assert score.carbon_component == Decimal("0.0045")
    assert score.objective == Decimal("0.174500")


def test_score_carbon_objective_rejects_float_cost() -> None:
    with pytest.raises(ValueError, match="cost must be a Decimal"):
        score_carbon_objective(
            cost=0.1,
            latency_ms=Decimal("250"),
            carbon_intensity_gco2_per_kwh=Decimal("45"),
            weights=CarbonObjectiveWeights(),
        )


def test_score_carbon_objective_rejects_negative_weight() -> None:
    with pytest.raises(ValueError, match="carbon_weight must be non-negative"):
        CarbonObjectiveWeights(carbon_weight=Decimal("-0.0001"))
