"""Snapshot-style route planner contract for a BGE-M3 chain — ."""

from __future__ import annotations

from datetime import UTC, datetime

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.routing import Hints, RoutingRequest, plan_route

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _capability() -> Capability:
    return Capability(
        id="cap_bge_m3",
        name="embedding.bge-m3",
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode="per_second",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider(
    provider_id: str,
    *,
    provider_type: ProviderType,
    priority: int,
    config: dict[str, object],
    region: str | None,
    cold_start_p50_ms: int | None,
    recent_error_rate: float = 0.0,
) -> Provider:
    return Provider(
        id=provider_id,
        capability_id="cap_bge_m3",
        name=provider_id,
        provider_type=provider_type,
        region=region,
        config=config,
        priority=priority,
        health_status="healthy",
        cold_start_p50_ms=cold_start_p50_ms,
        recent_error_rate=recent_error_rate,
        updated_at=_NOW,
    )


def test_bge_m3_route_plan_snapshot_with_public_endpoint_fallback() -> None:
    request = RoutingRequest(
        capability_name="embedding.bge-m3",
        hints=Hints(
            latency_sensitive=True,
            cost_sensitive=True,
            region_preference="US-KS-2",
        ),
    )
    primary = _provider(
        "prov_bge_m3_primary",
        provider_type=ProviderType.SERVERLESS_QUEUE,
        priority=1,
        region="US-KS-2",
        cold_start_p50_ms=1_000,
        recent_error_rate=0.1,
        config={
            "warm_workers": 1,
            "cost": {"per_second_active": "0.001"},
            "priority_multiplier": "1.25",
            "fallback_chain": ["prov_bge_m3_public"],
        },
    )
    public_fallback = _provider(
        "prov_bge_m3_public",
        provider_type=ProviderType.PUBLIC_ENDPOINT,
        priority=2,
        region="US-KS-2",
        cold_start_p50_ms=0,
        config={
            "cost": {"per_second_active": "0.002"},
            "fallback_for": ["prov_bge_m3_primary"],
        },
    )

    plan = plan_route(
        request,
        [primary, public_fallback],
        capability=_capability(),
        now=_NOW,
    )

    assert plan.to_dict() == {
        "selected_provider_id": "prov_bge_m3_primary",
        "fallback_chain": ["prov_bge_m3_primary", "prov_bge_m3_public"],
        "fallback_provider_ids": ["prov_bge_m3_public"],
        "attempts": [
            {
                "attempt": 1,
                "provider_id": "prov_bge_m3_primary",
                "score": 137.5,
                "backoff_before_attempt_s": 0.0,
                "score_explanation": {
                    "provider_id": "prov_bge_m3_primary",
                    "base_score": 100.0,
                    "latency_penalty": 10.0,
                    "warm_worker_bonus": 20.0,
                    "cost_penalty": 10.0,
                    "region_bonus": 15.0,
                    "recent_error_penalty": 5.0,
                    "priority_multiplier": 1.25,
                    "score_before_multiplier": 110.0,
                    "final_score": 137.5,
                },
            },
            {
                "attempt": 2,
                "provider_id": "prov_bge_m3_public",
                "score": 95.0,
                "backoff_before_attempt_s": 1.0,
                "score_explanation": {
                    "provider_id": "prov_bge_m3_public",
                    "base_score": 100.0,
                    "latency_penalty": 0.0,
                    "warm_worker_bonus": 0.0,
                    "cost_penalty": 20.0,
                    "region_bonus": 15.0,
                    "recent_error_penalty": 0.0,
                    "priority_multiplier": 1.0,
                    "score_before_multiplier": 95.0,
                    "final_score": 95.0,
                },
            },
        ],
        "ranked_candidates": [
            {
                "provider_id": "prov_bge_m3_primary",
                "rank": 1,
                "score": 137.5,
                "fallback_for": [],
                "explicit_fallback_chain": ["prov_bge_m3_public"],
                "score_explanation": {
                    "provider_id": "prov_bge_m3_primary",
                    "base_score": 100.0,
                    "latency_penalty": 10.0,
                    "warm_worker_bonus": 20.0,
                    "cost_penalty": 10.0,
                    "region_bonus": 15.0,
                    "recent_error_penalty": 5.0,
                    "priority_multiplier": 1.25,
                    "score_before_multiplier": 110.0,
                    "final_score": 137.5,
                },
            },
            {
                "provider_id": "prov_bge_m3_public",
                "rank": 2,
                "score": 95.0,
                "fallback_for": ["prov_bge_m3_primary"],
                "explicit_fallback_chain": [],
                "score_explanation": {
                    "provider_id": "prov_bge_m3_public",
                    "base_score": 100.0,
                    "latency_penalty": 0.0,
                    "warm_worker_bonus": 0.0,
                    "cost_penalty": 20.0,
                    "region_bonus": 15.0,
                    "recent_error_penalty": 0.0,
                    "priority_multiplier": 1.0,
                    "score_before_multiplier": 95.0,
                    "final_score": 95.0,
                },
            },
        ],
        "eliminated": [],
        "dropped_provider_reasons": {},
        "capacity_decisions": [],
        "max_attempts": 3,
    }
