"""Property-based tests for the pure route planner (routing/planner.py).

Grounded in src/pitwall/routing/{planner,types}.py (verified 2026-05-30):
    plan_route(request, providers, *, capability=None, now=None,
               max_attempts=3, backoff_base_s=1.0, ...) -> RoutePlan
RoutePlan.ranked_candidates / .eliminated / .attempts each carry provider_id;
.to_dict() is order-stable and JSON-serializable. `_validate_plan_options`
raises ValueError for max_attempts < 1.

To keep the planner PURE and deterministic we generate only NON-pod-lease
providers (SERVERLESS_QUEUE / PUBLIC_ENDPOINT). Stage 4 only probes pod-lease
providers via the global availability cache, so excluding them means plan_route
never touches external state — every property below is reproducible.

Invariants:
    1. Determinism: identical inputs -> identical RoutePlan.to_dict()
    2. Partition: every input provider id is in ranked_candidates XOR eliminated
       (never both, never lost)
    3. Bounded attempts: attempt ids subset of ranked ids; len <= max_attempts
    4. Ranking is non-increasing by score with no NaN
    5. Disabled / unhealthy providers never appear in the attempt chain
    6. max_attempts < 1 raises ValueError
"""

from __future__ import annotations

import datetime as dt
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.routing import RoutingRequest, plan_route

pytestmark = pytest.mark.property

_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)
_CAP_ID = "cap_bge_m3"
_CAP_NAME = "embedding.bge-m3"
_NON_POD_TYPES = [ProviderType.SERVERLESS_QUEUE, ProviderType.PUBLIC_ENDPOINT]


def _capability() -> Capability:
    return Capability(
        id=_CAP_ID,
        name=_CAP_NAME,
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode="per_second",
        created_at=_NOW,
        updated_at=_NOW,
    )


@st.composite
def provider_lists(draw: st.DrawFn) -> list[Provider]:
    n = draw(st.integers(min_value=1, max_value=6))
    providers: list[Provider] = []
    for i in range(n):
        ptype = draw(st.sampled_from(_NON_POD_TYPES))
        enabled = draw(st.booleans())
        health = draw(st.sampled_from(["healthy", "degraded", "unhealthy"]))
        providers.append(
            Provider(
                id=f"prov_{i}",
                capability_id=_CAP_ID,
                name=f"prov_{i}",
                provider_type=ptype,
                config={"cost": {"per_second_active": "0.001"}},
                priority=draw(st.integers(min_value=1, max_value=10)),
                enabled=enabled,
                health_status=health,
                cold_start_p50_ms=draw(st.integers(min_value=100, max_value=30_000)),
                recent_error_rate=draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False)),
                updated_at=_NOW,
            )
        )
    return providers


def _request() -> RoutingRequest:
    return RoutingRequest(capability_name=_CAP_NAME, capability_id=_CAP_ID)


@given(providers=provider_lists())
def test_determinism(providers: list[Provider]) -> None:
    cap = _capability()
    a = plan_route(_request(), providers, capability=cap, now=_NOW)
    b = plan_route(_request(), providers, capability=cap, now=_NOW)
    assert a.to_dict() == b.to_dict()


@given(providers=provider_lists())
def test_partition_every_provider_ranked_xor_eliminated(
    providers: list[Provider],
) -> None:
    plan = plan_route(_request(), providers, capability=_capability(), now=_NOW)
    input_ids = {p.id for p in providers}
    ranked_ids = {c.provider_id for c in plan.ranked_candidates}
    eliminated_ids = {e.provider_id for e in plan.eliminated}
    assert ranked_ids.isdisjoint(eliminated_ids)
    assert ranked_ids <= input_ids
    assert eliminated_ids <= input_ids
    # no provider is silently lost
    assert ranked_ids | eliminated_ids == input_ids


@given(providers=provider_lists(), max_attempts=st.integers(min_value=1, max_value=5))
def test_attempts_bounded_and_subset_of_ranked(
    providers: list[Provider], max_attempts: int
) -> None:
    plan = plan_route(
        _request(), providers, capability=_capability(), now=_NOW, max_attempts=max_attempts
    )
    ranked_ids = {c.provider_id for c in plan.ranked_candidates}
    attempt_ids = [a.provider_id for a in plan.attempts]
    assert len(attempt_ids) <= max_attempts
    assert set(attempt_ids) <= ranked_ids


@given(providers=provider_lists())
def test_ranking_non_increasing_and_finite(providers: list[Provider]) -> None:
    plan = plan_route(_request(), providers, capability=_capability(), now=_NOW)
    scores = [c.score for c in plan.ranked_candidates]
    for s in scores:
        assert not math.isnan(s)
    assert scores == sorted(scores, reverse=True)


@given(providers=provider_lists())
def test_disabled_or_unhealthy_never_attempted(providers: list[Provider]) -> None:
    plan = plan_route(_request(), providers, capability=_capability(), now=_NOW)
    by_id = {p.id: p for p in providers}
    for attempt in plan.attempts:
        prov = by_id[attempt.provider_id]
        assert prov.enabled is True
        assert prov.health_status != "unhealthy"


@given(bad=st.integers(max_value=0))
def test_max_attempts_below_one_raises(bad: int) -> None:
    with pytest.raises(ValueError):
        plan_route(_request(), [], capability=_capability(), now=_NOW, max_attempts=bad)
