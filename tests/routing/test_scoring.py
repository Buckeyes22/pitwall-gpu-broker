"""Tests for Stage 3 hint-based provider scoring — ."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pitwall.core.enums import ProviderType
from pitwall.core.models import Provider
from pitwall.routing import Hints, ObservedMetrics, score_provider

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _provider(
    *,
    region: str | None = "US-KS-2",
    config: dict[str, object] | None = None,
    priority: int = 0,
    cold_start_p50_ms: int | None = None,
    recent_error_rate: float = 0.0,
) -> Provider:
    return Provider(
        id="prov_scoring",
        capability_id="cap_embed",
        name="scoring-provider",
        provider_type=ProviderType.SERVERLESS_QUEUE,
        region=region,
        config=config or {},
        priority=priority,
        cold_start_p50_ms=cold_start_p50_ms,
        recent_error_rate=recent_error_rate,
        updated_at=_NOW,
    )


def test_baseline_score_is_neutral_without_hints_or_observed_errors() -> None:
    provider = _provider(region=None)

    assert score_provider(provider, Hints(), ObservedMetrics()) == 100.0


def test_latency_hint_subtracts_cold_start_p50_ms() -> None:
    provider = _provider(cold_start_p50_ms=8_000)

    assert score_provider(provider, Hints(latency_sensitive=True)) == 20.0


def test_latency_hint_adds_warm_worker_bonus() -> None:
    provider = _provider(
        cold_start_p50_ms=1_000,
        config={"warm_workers": 1},
    )

    assert score_provider(provider, Hints(latency_sensitive=True)) == 110.0


def test_latency_hint_does_not_add_warm_bonus_without_warm_workers() -> None:
    provider = _provider(
        cold_start_p50_ms=1_000,
        config={"warm_workers": 0},
    )

    assert score_provider(provider, Hints(latency_sensitive=True)) == 90.0


def test_cost_hint_subtracts_per_second_active_cost_from_provider_config() -> None:
    provider = _provider(config={"cost": {"per_second_active": Decimal("0.000123")}})

    assert score_provider(provider, Hints(cost_sensitive=True)) == pytest.approx(98.77)


def test_region_preference_adds_region_match_bonus() -> None:
    provider = _provider(region="US-CA-2")

    assert score_provider(provider, Hints(region_preference="US-CA-2")) == 115.0


def test_missing_region_preference_does_not_bonus_missing_provider_region() -> None:
    provider = _provider(region=None)

    assert score_provider(provider, Hints(region_preference=None)) == 100.0


def test_recent_error_rate_penalty_is_always_applied() -> None:
    provider = _provider(recent_error_rate=0.2)

    assert score_provider(provider, Hints()) == 90.0


def test_observed_recent_error_rate_is_fallback_for_provider_shaped_mapping() -> None:
    provider = {"id": "prov_mapping"}

    assert score_provider(provider, observed=ObservedMetrics(recent_error_rate=0.3)) == 85.0


def test_priority_multiplier_applies_after_additions_and_penalties() -> None:
    provider = _provider(
        region="US-KS-2",
        cold_start_p50_ms=1_000,
        recent_error_rate=0.1,
        config={
            "warm_workers": 2,
            "cost": {"per_second_active": "0.001"},
            "priority_multiplier": "1.25",
        },
    )

    result = score_provider(
        provider,
        Hints(
            latency_sensitive=True,
            cost_sensitive=True,
            region_preference="US-KS-2",
        ),
    )

    assert result == pytest.approx(137.5)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("cold_start_p50_ms", -1, "cold_start_p50_ms"),
        ("warm_workers", -1, "warm_workers"),
        ("cost", {"per_second_active": -1}, "cost_per_second_active"),
        ("recent_error_rate", 1.1, "recent_error_rate"),
        ("priority_multiplier", -0.5, "priority_multiplier"),
    ],
)
def test_invalid_numeric_terms_are_rejected(
    field: str,
    value: object,
    match: str,
) -> None:
    provider = {
        "id": "prov_invalid",
        field: value,
    }

    hints = Hints(
        latency_sensitive=True,
        cost_sensitive=True,
    )

    with pytest.raises(ValueError, match=match):
        score_provider(provider, hints)
