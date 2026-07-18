"""Tests for explicit fallback-chain planning — /."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.routing import Hints, ProviderEliminated, RoutingRequest, plan_route

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _provider(
    provider_id: str,
    *,
    name: str | None = None,
    provider_type: ProviderType = ProviderType.SERVERLESS_QUEUE,
    priority: int = 1,
    region: str | None = "US-KS-2",
    config: dict[str, object] | None = None,
    health_status: str = "healthy",
    cooldown_until: datetime | None = None,
    cold_start_p50_ms: int | None = None,
    recent_error_rate: float = 0.0,
    enabled: bool = True,
) -> Provider:
    return Provider(
        id=provider_id,
        capability_id="cap_embed",
        name=name or provider_id,
        provider_type=provider_type,
        region=region,
        config=config or {},
        priority=priority,
        enabled=enabled,
        health_status=health_status,
        cooldown_until=cooldown_until,
        cold_start_p50_ms=cold_start_p50_ms,
        recent_error_rate=recent_error_rate,
        updated_at=_NOW,
    )


def _capability() -> Capability:
    return Capability(
        id="cap_embed",
        name="embedding.bge-m3",
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode="per_second",
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_explicit_fallback_chain_order_is_preserved_and_capped_at_three_attempts() -> None:
    request = RoutingRequest(
        capability_name="embedding.bge-m3",
        hints=Hints(latency_sensitive=True),
    )
    providers = [
        _provider(
            "prov_primary",
            config={"fallback_chain": ["prov_fallback_b", "prov_fallback_a", "prov_fallback_c"]},
            cold_start_p50_ms=500,
        ),
        _provider("prov_fallback_a", priority=2, cold_start_p50_ms=0),
        _provider("prov_fallback_b", priority=3, cold_start_p50_ms=1_000),
        _provider("prov_fallback_c", priority=4, cold_start_p50_ms=0),
    ]

    plan = plan_route(
        request,
        providers,
        capability=_capability(),
        now=_NOW,
    )

    assert plan.selected_provider_id == "prov_primary"
    assert plan.fallback_chain == ("prov_primary", "prov_fallback_b", "prov_fallback_a")
    assert [attempt.backoff_before_attempt_s for attempt in plan.attempts] == [0.0, 1.0, 2.0]


def test_fallback_for_providers_are_ordered_by_provider_priority_then_score() -> None:
    request = RoutingRequest(capability_name="embedding.bge-m3")
    providers = [
        _provider("prov_primary", priority=1),
        _provider(
            "prov_fallback_low_priority",
            priority=3,
            config={"fallback_for": ["prov_primary"]},
        ),
        _provider(
            "prov_fallback_high_priority",
            priority=2,
            config={"fallback_for": ["prov_primary"]},
        ),
    ]

    plan = plan_route(request, providers, capability=_capability(), now=_NOW)

    assert plan.fallback_chain == (
        "prov_primary",
        "prov_fallback_high_priority",
        "prov_fallback_low_priority",
    )


def test_no_inferred_fallback_chain_when_provider_config_is_not_explicit() -> None:
    request = RoutingRequest(capability_name="embedding.bge-m3")
    providers = [
        _provider("prov_a", priority=1),
        _provider("prov_b", priority=2),
    ]

    plan = plan_route(request, providers, capability=_capability(), now=_NOW)

    assert plan.fallback_chain == ("prov_a",)
    assert plan.fallback_provider_ids == ()


def test_stage2_health_and_cooldown_eliminations_are_reported() -> None:
    request = RoutingRequest(capability_name="embedding.bge-m3")
    providers = [
        _provider("prov_ok"),
        _provider("prov_unhealthy", health_status="unhealthy"),
        _provider("prov_cooling", cooldown_until=_NOW + timedelta(minutes=3)),
    ]

    plan = plan_route(request, providers, capability=_capability(), now=_NOW)

    assert plan.fallback_chain == ("prov_ok",)
    assert plan.dropped_provider_reasons == {
        "prov_unhealthy": [ProviderEliminated.HEALTH_UNHEALTHY.value],
        "prov_cooling": [ProviderEliminated.HEALTH_COOLDOWN.value],
    }


def test_explicit_fallback_chain_skips_non_existent_provider_ids() -> None:
    request = RoutingRequest(capability_name="embedding.bge-m3")
    providers = [
        _provider(
            "prov_primary",
            config={
                "fallback_chain": ["prov_nonexistent", "prov_fallback_a", "prov_also_nonexistent"]
            },
        ),
        _provider("prov_fallback_a"),
    ]

    plan = plan_route(request, providers, capability=_capability(), now=_NOW)

    assert plan.fallback_chain == ("prov_primary", "prov_fallback_a")


def test_explicit_fallback_chain_deduplicates_provider_ids() -> None:
    request = RoutingRequest(capability_name="embedding.bge-m3")
    providers = [
        _provider(
            "prov_primary",
            config={"fallback_chain": ["prov_fallback_a", "prov_fallback_b", "prov_fallback_a"]},
        ),
        _provider("prov_fallback_a"),
        _provider("prov_fallback_b"),
    ]

    plan = plan_route(request, providers, capability=_capability(), now=_NOW)

    assert plan.fallback_chain == ("prov_primary", "prov_fallback_a", "prov_fallback_b")


def test_max_attempts_one_returns_only_primary() -> None:
    request = RoutingRequest(capability_name="embedding.bge-m3")
    providers = [
        _provider(
            "prov_primary",
            config={"fallback_chain": ["prov_fallback_a", "prov_fallback_b"]},
        ),
        _provider("prov_fallback_a"),
        _provider("prov_fallback_b"),
    ]

    plan = plan_route(request, providers, capability=_capability(), now=_NOW, max_attempts=1)

    assert plan.fallback_chain == ("prov_primary",)
    assert plan.fallback_provider_ids == ()
    assert len(plan.attempts) == 1


def test_fallback_candidates_eliminated_in_stage2_still_allow_lower_priority_chain() -> None:
    request = RoutingRequest(capability_name="embedding.bge-m3")
    providers = [
        _provider("prov_primary", priority=1),
        _provider(
            "prov_fallback_high_priority",
            priority=2,
            health_status="unhealthy",
            config={"fallback_for": ["prov_primary"]},
        ),
        _provider(
            "prov_fallback_low_priority",
            priority=3,
            config={"fallback_for": ["prov_primary"]},
        ),
    ]

    plan = plan_route(request, providers, capability=_capability(), now=_NOW)

    assert plan.fallback_chain == ("prov_primary", "prov_fallback_low_priority")
    assert "prov_fallback_high_priority" in plan.dropped_provider_reasons


def test_primary_selected_over_provider_that_is_explicit_fallback_target() -> None:
    request = RoutingRequest(capability_name="embedding.bge-m3")
    providers = [
        _provider(
            "prov_primary",
            priority=1,
            config={"fallback_chain": ["prov_secondary"]},
        ),
        _provider("prov_secondary", priority=2),
    ]

    plan = plan_route(request, providers, capability=_capability(), now=_NOW)

    assert plan.selected_provider_id == "prov_primary"
    assert plan.fallback_chain == ("prov_primary", "prov_secondary")
