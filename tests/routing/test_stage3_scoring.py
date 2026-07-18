"""Acceptance coverage for Stage 3 score terms — /."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pitwall.core.enums import ProviderType
from pitwall.core.models import Provider
from pitwall.routing import Hints, ObservedMetrics, explain_score, score_provider

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _provider(
    *,
    region: str | None = "US-KS-2",
    config: dict[str, object] | None = None,
    cold_start_p50_ms: int | None = None,
    recent_error_rate: float = 0.0,
) -> Provider:
    return Provider(
        id="prov_stage3",
        capability_id="cap_embed",
        name="stage3-provider",
        provider_type=ProviderType.SERVERLESS_QUEUE,
        region=region,
        config=config or {},
        priority=1,
        cold_start_p50_ms=cold_start_p50_ms,
        recent_error_rate=recent_error_rate,
        updated_at=_NOW,
    )


def test_stage3_score_includes_latency_cost_region_error_and_multiplier() -> None:
    provider = _provider(
        region="US-KS-2",
        cold_start_p50_ms=1_000,
        recent_error_rate=0.1,
        config={
            "warm_workers": 1,
            "cost": {"per_second_active": "0.001"},
            "priority_multiplier": "1.25",
        },
    )

    score = score_provider(
        provider,
        Hints(
            latency_sensitive=True,
            cost_sensitive=True,
            region_preference="US-KS-2",
        ),
    )

    assert score == pytest.approx(137.5)


def test_explain_score_returns_each_formula_term() -> None:
    provider = _provider(
        region="US-KS-2",
        cold_start_p50_ms=2_000,
        recent_error_rate=0.2,
        config={
            "warm_workers": 1,
            "cost": {"per_second_active": "0.002"},
        },
    )

    explanation = explain_score(
        provider,
        Hints(
            latency_sensitive=True,
            cost_sensitive=True,
            region_preference="US-KS-2",
        ),
        ObservedMetrics(),
    )

    assert explanation.to_dict() == {
        "provider_id": "prov_stage3",
        "base_score": 100.0,
        "latency_penalty": 20.0,
        "warm_worker_bonus": 20.0,
        "cost_penalty": 20.0,
        "region_bonus": 15.0,
        "recent_error_penalty": 10.0,
        "priority_multiplier": 1.0,
        "score_before_multiplier": 85.0,
        "final_score": 85.0,
    }
