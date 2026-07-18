"""Tests for demand-forecast prewarm recommendations."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from pitwall.core.enums import ProviderType
from pitwall.core.models import Provider
from pitwall.routing import (
    DemandSample,
    PrewarmPolicy,
    PrewarmTargetKind,
    forecast_demand,
    plan_prewarm,
)

_NOW = dt.datetime(2026, 6, 2, 12, 0, 0, tzinfo=dt.UTC)


def _provider(
    provider_id: str,
    *,
    capability_id: str = "cap_embed",
    provider_type: ProviderType = ProviderType.SERVERLESS_LB,
    priority: int = 1,
    config: dict[str, object] | None = None,
    runpod_endpoint_id: str | None = "endpoint_embed",
    runpod_template_id: str | None = None,
    health_status: str = "healthy",
    enabled: bool = True,
    cooldown_until: dt.datetime | None = None,
    cold_start_p95_ms: int | None = None,
) -> Provider:
    return Provider(
        id=provider_id,
        capability_id=capability_id,
        name=provider_id,
        provider_type=provider_type,
        runpod_endpoint_id=runpod_endpoint_id,
        runpod_template_id=runpod_template_id,
        config=config or {},
        priority=priority,
        enabled=enabled,
        health_status=health_status,
        cooldown_until=cooldown_until,
        cold_start_p95_ms=cold_start_p95_ms,
        updated_at=_NOW,
    )


def _sample(minutes_before_now: int, count: int, capability_id: str = "cap_embed") -> DemandSample:
    return DemandSample(
        capability_id=capability_id,
        observed_at=_NOW - dt.timedelta(minutes=minutes_before_now),
        request_count=count,
    )


def test_forecast_buckets_recent_counts_by_capability_and_applies_headroom() -> None:
    policy = PrewarmPolicy(
        lookback=dt.timedelta(minutes=15),
        sample_window=dt.timedelta(minutes=5),
        forecast_window=dt.timedelta(minutes=5),
        forecast_horizon=dt.timedelta(minutes=5),
        headroom=1.2,
    )

    forecasts = forecast_demand(
        [
            _sample(14, 4),
            _sample(6, 6),
            _sample(1, 10),
            _sample(30, 999),
            _sample(1, 7, capability_id="cap_vision"),
        ],
        now=_NOW,
        policy=policy,
    )

    by_capability = {forecast.capability_id: forecast for forecast in forecasts}

    assert by_capability["cap_embed"].observed_counts == (4, 6, 10)
    assert by_capability["cap_embed"].projected_requests == 16
    assert by_capability["cap_embed"].window_start == _NOW + dt.timedelta(minutes=5)
    assert by_capability["cap_embed"].window_end == _NOW + dt.timedelta(minutes=10)
    assert by_capability["cap_vision"].observed_counts == (0, 0, 7)


def test_prewarm_plan_is_deterministic_for_unsorted_history_and_providers() -> None:
    policy = PrewarmPolicy(
        lookback=dt.timedelta(minutes=15),
        sample_window=dt.timedelta(minutes=5),
        forecast_window=dt.timedelta(minutes=5),
        headroom=1.0,
        default_requests_per_warm_unit=10,
    )
    history = [
        _sample(1, 20, "cap_vision"),
        _sample(11, 10),
        _sample(2, 20),
        _sample(8, 10, "cap_vision"),
    ]
    providers = [
        _provider("prov_vision", capability_id="cap_vision", runpod_endpoint_id="endpoint_vision"),
        _provider("prov_embed", runpod_endpoint_id="endpoint_embed"),
    ]

    first = plan_prewarm(history, providers, now=_NOW, policy=policy)
    second = plan_prewarm(reversed(history), reversed(providers), now=_NOW, policy=policy)

    assert first.to_dict() == second.to_dict()


def test_forecast_rejects_naive_now_and_negative_counts() -> None:
    policy = PrewarmPolicy()

    with pytest.raises(ValueError, match="now"):
        forecast_demand([], now=dt.datetime(2026, 6, 2, 12, 0, 0), policy=policy)

    with pytest.raises(ValueError, match="request_count"):
        forecast_demand([_sample(1, -1)], now=_NOW, policy=policy)


def test_plan_recommends_endpoint_workers_for_top_healthy_provider() -> None:
    policy = PrewarmPolicy(
        lookback=dt.timedelta(minutes=15),
        sample_window=dt.timedelta(minutes=5),
        forecast_window=dt.timedelta(minutes=5),
        headroom=1.0,
        default_requests_per_warm_unit=15,
    )
    providers = [
        _provider(
            "prov_slow",
            priority=2,
            config={"workers": {"workers_min": 0}},
            runpod_endpoint_id="endpoint_slow",
        ),
        _provider(
            "prov_fast",
            priority=1,
            config={"workers": {"workers_min": 1}},
            runpod_endpoint_id="endpoint_fast",
        ),
    ]

    plan = plan_prewarm(
        [_sample(14, 30), _sample(7, 30), _sample(1, 30)],
        providers,
        now=_NOW,
        policy=policy,
    )

    assert len(plan.recommendations) == 1
    recommendation = plan.recommendations[0]
    assert recommendation.provider_id == "prov_fast"
    assert recommendation.target_kind == PrewarmTargetKind.ENDPOINT_WORKERS
    assert recommendation.current_warm_count == 1
    assert recommendation.target_count == 2
    assert recommendation.delta == 1
    assert recommendation.target["runpod_endpoint_id"] == "endpoint_fast"
    assert recommendation.start_at == _NOW
    assert recommendation.ready_by == _NOW + dt.timedelta(minutes=5)


def test_plan_recommends_pod_targets_with_gpu_shape() -> None:
    policy = PrewarmPolicy(
        lookback=dt.timedelta(minutes=10),
        sample_window=dt.timedelta(minutes=5),
        forecast_window=dt.timedelta(minutes=5),
        headroom=1.0,
        default_requests_per_warm_unit=10,
    )
    provider = _provider(
        "prov_pod",
        provider_type=ProviderType.POD_LEASE,
        runpod_endpoint_id=None,
        runpod_template_id="template_embed",
        config={
            "warm_pods": 0,
            "gpu_type_priority": ["NVIDIA L4"],
            "gpu_count": 1,
            "dataCenterIds": ["US-KS-2"],
            "cloud_type": "SECURE",
        },
    )

    plan = plan_prewarm([_sample(7, 20), _sample(1, 20)], [provider], now=_NOW, policy=policy)

    recommendation = plan.recommendations[0]
    assert recommendation.target_kind == PrewarmTargetKind.POD_LEASE
    assert recommendation.target_count == 2
    assert recommendation.target == {
        "runpod_template_id": "template_embed",
        "datacenter": "US-KS-2",
        "gpu_name": "NVIDIA L4",
        "gpu_count": 1,
        "cloud_type": "SECURE",
    }


def test_plan_skips_recommendation_when_current_warm_capacity_covers_forecast() -> None:
    policy = PrewarmPolicy(
        lookback=dt.timedelta(minutes=10),
        sample_window=dt.timedelta(minutes=5),
        forecast_window=dt.timedelta(minutes=5),
        headroom=1.0,
        default_requests_per_warm_unit=10,
    )
    provider = _provider(
        "prov_warm",
        config={"workers": {"workers_min": 3}},
    )

    plan = plan_prewarm([_sample(6, 10), _sample(1, 10)], [provider], now=_NOW, policy=policy)

    assert plan.recommendations == ()


def test_plan_filters_disabled_unhealthy_cooling_and_unsupported_providers() -> None:
    policy = PrewarmPolicy(
        lookback=dt.timedelta(minutes=10),
        sample_window=dt.timedelta(minutes=5),
        forecast_window=dt.timedelta(minutes=5),
        headroom=1.0,
        default_requests_per_warm_unit=10,
        max_targets_per_capability=4,
    )
    providers = [
        _provider("prov_disabled", enabled=False),
        _provider("prov_unhealthy", health_status="unhealthy"),
        _provider("prov_cooling", cooldown_until=_NOW + dt.timedelta(minutes=5)),
        _provider("prov_public", provider_type=ProviderType.PUBLIC_ENDPOINT),
        _provider("prov_ready", priority=5),
    ]

    plan = plan_prewarm([_sample(6, 30), _sample(1, 30)], providers, now=_NOW, policy=policy)

    assert [recommendation.provider_id for recommendation in plan.recommendations] == ["prov_ready"]


def test_prewarm_plan_to_dict_is_json_serializable_and_does_not_emit_runpod_urls() -> None:
    policy = PrewarmPolicy(
        lookback=dt.timedelta(minutes=10),
        sample_window=dt.timedelta(minutes=5),
        forecast_window=dt.timedelta(minutes=5),
        headroom=1.0,
        default_requests_per_warm_unit=10,
    )

    plan = plan_prewarm(
        [_sample(6, 30), _sample(1, 30)],
        [_provider("prov_lb", runpod_endpoint_id="endpoint_safe")],
        now=_NOW,
        policy=policy,
    )

    payload = plan.to_dict()
    encoded = json.dumps(payload, sort_keys=True)

    assert "endpoint_safe" in encoded
    assert "https://" not in encoded
