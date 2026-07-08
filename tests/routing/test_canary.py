"""Hermetic tests for shadow/canary routing and auto-promotion decisions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from pitwall.routing.canary import (
    CanaryComparison,
    CanaryDecisionAction,
    CanaryMode,
    CanaryObservation,
    CanaryPromotionPolicy,
    CanaryProviderRequest,
    CanaryRoutingPolicy,
    ProviderMetrics,
    evaluate_canary,
    route_with_canary,
)


@dataclass(frozen=True, slots=True)
class FakeProvider:
    id: str


@dataclass(slots=True)
class AttemptLog:
    called: list[str] = field(default_factory=list)
    observed: list[str] = field(default_factory=list)


ProviderCallable = Callable[[FakeProvider], Awaitable[str]]


def _provider(provider_id: str) -> FakeProvider:
    return FakeProvider(id=provider_id)


def _caller(responses: dict[str, str], log: AttemptLog) -> ProviderCallable:
    async def call(provider: FakeProvider) -> str:
        log.called.append(provider.id)
        return responses[provider.id]

    return call


def _observer(
    *,
    latency_ms: dict[str, Decimal] | None = None,
    cost_usd: dict[str, Decimal] | None = None,
    quality_score: dict[str, float] | None = None,
    log: AttemptLog,
) -> Callable[[FakeProvider, str], Awaitable[CanaryObservation]]:
    async def observe(provider: FakeProvider, value: str) -> CanaryObservation:
        log.observed.append(f"{provider.id}:{value}")
        return CanaryObservation(
            provider_id=provider.id,
            success=True,
            latency_ms=(latency_ms or {}).get(provider.id),
            cost_usd=(cost_usd or {}).get(provider.id),
            quality_score=(quality_score or {}).get(provider.id),
        )

    return observe


def _observer_with_candidate_event(
    log: AttemptLog,
    candidate_observed: asyncio.Event,
) -> Callable[[FakeProvider, str], Awaitable[CanaryObservation]]:
    async def observe(provider: FakeProvider, value: str) -> CanaryObservation:
        observation = await _observer(log=log)(provider, value)
        if provider.id == "candidate":
            candidate_observed.set()
        return observation

    return observe


def _metrics(
    provider_id: str,
    *,
    successes: int,
    failures: int = 0,
    latency_ms: Decimal,
    cost_usd: Decimal,
    quality_score: float,
) -> ProviderMetrics:
    observations = [
        CanaryObservation(
            provider_id=provider_id,
            success=True,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            quality_score=quality_score,
        )
        for _ in range(successes)
    ]
    observations.extend(
        CanaryObservation(provider_id=provider_id, success=False) for _ in range(failures)
    )
    return ProviderMetrics.from_observations(provider_id, observations)


@pytest.mark.anyio
async def test_shadow_routes_candidate_but_serves_baseline_result() -> None:
    log = AttemptLog()
    candidate_observed = asyncio.Event()

    result = await route_with_canary(
        CanaryProviderRequest(
            baseline_provider=_provider("baseline"),
            candidate_provider=_provider("candidate"),
            call_provider=_caller(
                {"baseline": "served:baseline", "candidate": "ignored:candidate"},
                log,
            ),
            policy=CanaryRoutingPolicy(
                mode=CanaryMode.SHADOW,
                candidate_fraction=1.0,
                experiment_id="exp-shadow",
            ),
            traffic_key="request-1",
            traffic_bucket=0.0,
            observe_result=_observer_with_candidate_event(log, candidate_observed),
        )
    )

    assert result.value == "served:baseline"
    assert result.served_provider_id == "baseline"
    assert result.candidate_selected is True
    assert result.shadowed is True
    assert [item.provider_id for item in result.observations] == ["baseline"]
    assert result.candidate_observation is None
    await asyncio.wait_for(candidate_observed.wait(), timeout=1)
    assert set(log.called) == {"baseline", "candidate"}


@pytest.mark.anyio
async def test_shadow_returns_baseline_before_slow_candidate_finishes() -> None:
    log = AttemptLog()
    candidate_started = asyncio.Event()
    release_candidate = asyncio.Event()
    candidate_observed = asyncio.Event()

    async def call(provider: FakeProvider) -> str:
        log.called.append(provider.id)
        if provider.id == "candidate":
            candidate_started.set()
            await release_candidate.wait()
            return "ignored:candidate"
        return "served:baseline"

    result = await asyncio.wait_for(
        route_with_canary(
            CanaryProviderRequest(
                baseline_provider=_provider("baseline"),
                candidate_provider=_provider("candidate"),
                call_provider=call,
                policy=CanaryRoutingPolicy(
                    mode=CanaryMode.SHADOW,
                    candidate_fraction=1.0,
                    experiment_id="exp-shadow",
                ),
                traffic_key="request-slow-shadow",
                traffic_bucket=0.0,
                observe_result=_observer_with_candidate_event(log, candidate_observed),
            )
        ),
        timeout=0.05,
    )

    assert result.value == "served:baseline"
    assert result.served_provider_id == "baseline"
    assert [item.provider_id for item in result.observations] == ["baseline"]
    assert result.candidate_observation is None
    assert result.shadow_observation_task is not None

    await asyncio.wait_for(candidate_started.wait(), timeout=1)
    release_candidate.set()
    candidate_observation = await asyncio.wait_for(result.shadow_observation_task, timeout=1)
    assert candidate_observation == CanaryObservation(provider_id="candidate", success=True)
    assert candidate_observed.is_set()
    assert set(log.called) == {"baseline", "candidate"}


@pytest.mark.anyio
async def test_shadow_fraction_zero_does_not_call_candidate() -> None:
    log = AttemptLog()

    result = await route_with_canary(
        CanaryProviderRequest(
            baseline_provider=_provider("baseline"),
            candidate_provider=_provider("candidate"),
            call_provider=_caller(
                {"baseline": "served:baseline", "candidate": "unused"},
                log,
            ),
            policy=CanaryRoutingPolicy(
                mode=CanaryMode.SHADOW,
                candidate_fraction=0.0,
                experiment_id="exp-shadow",
            ),
            traffic_key="request-2",
            traffic_bucket=0.0,
            observe_result=_observer(log=log),
        )
    )

    assert result.value == "served:baseline"
    assert result.served_provider_id == "baseline"
    assert result.candidate_selected is False
    assert result.shadowed is False
    assert result.candidate_observation is None
    assert log.called == ["baseline"]


@pytest.mark.anyio
async def test_canary_bucket_below_fraction_serves_candidate_only() -> None:
    log = AttemptLog()

    result = await route_with_canary(
        CanaryProviderRequest(
            baseline_provider=_provider("baseline"),
            candidate_provider=_provider("candidate"),
            call_provider=_caller(
                {"baseline": "unused", "candidate": "served:candidate"},
                log,
            ),
            policy=CanaryRoutingPolicy(
                mode=CanaryMode.CANARY,
                candidate_fraction=0.25,
                experiment_id="exp-canary",
            ),
            traffic_key="request-3",
            traffic_bucket=0.24,
            observe_result=_observer(log=log),
        )
    )

    assert result.value == "served:candidate"
    assert result.served_provider_id == "candidate"
    assert result.candidate_selected is True
    assert result.shadowed is False
    assert result.baseline_observation is None
    assert log.called == ["candidate"]


@pytest.mark.anyio
async def test_canary_bucket_at_fraction_serves_baseline() -> None:
    log = AttemptLog()

    result = await route_with_canary(
        CanaryProviderRequest(
            baseline_provider=_provider("baseline"),
            candidate_provider=_provider("candidate"),
            call_provider=_caller(
                {"baseline": "served:baseline", "candidate": "unused"},
                log,
            ),
            policy=CanaryRoutingPolicy(
                mode=CanaryMode.CANARY,
                candidate_fraction=0.25,
                experiment_id="exp-canary",
            ),
            traffic_key="request-4",
            traffic_bucket=0.25,
            observe_result=_observer(log=log),
        )
    )

    assert result.value == "served:baseline"
    assert result.served_provider_id == "baseline"
    assert result.candidate_selected is False
    assert result.candidate_observation is None
    assert log.called == ["baseline"]


def test_auto_promote_when_candidate_beats_baseline_thresholds() -> None:
    baseline = _metrics(
        "baseline",
        successes=20,
        latency_ms=Decimal("100"),
        cost_usd=Decimal("0.020"),
        quality_score=0.80,
    )
    candidate = _metrics(
        "candidate",
        successes=20,
        latency_ms=Decimal("80"),
        cost_usd=Decimal("0.015"),
        quality_score=0.88,
    )

    decision = evaluate_canary(
        CanaryComparison(baseline=baseline, candidate=candidate),
        policy=CanaryPromotionPolicy(
            min_baseline_samples=10,
            min_candidate_samples=10,
            min_quality_delta=0.05,
            max_latency_ratio=0.90,
            max_cost_ratio=0.90,
        ),
    )

    assert decision.action is CanaryDecisionAction.PROMOTE
    assert decision.reason == "candidate_beats_baseline"
    assert "quality_delta:+0.080000" in decision.signals
    assert "latency_ratio:0.800000" in decision.signals
    assert "cost_ratio:0.750000" in decision.signals


def test_auto_promote_holds_until_candidate_has_enough_samples() -> None:
    decision = evaluate_canary(
        CanaryComparison(
            baseline=_metrics(
                "baseline",
                successes=10,
                latency_ms=Decimal("100"),
                cost_usd=Decimal("0.020"),
                quality_score=0.80,
            ),
            candidate=_metrics(
                "candidate",
                successes=2,
                latency_ms=Decimal("60"),
                cost_usd=Decimal("0.010"),
                quality_score=0.95,
            ),
        ),
        policy=CanaryPromotionPolicy(min_baseline_samples=10, min_candidate_samples=5),
    )

    assert decision.action is CanaryDecisionAction.HOLD
    assert decision.reason == "insufficient_candidate_samples"


def test_auto_promote_rolls_back_when_candidate_violates_success_guardrail() -> None:
    decision = evaluate_canary(
        CanaryComparison(
            baseline=_metrics(
                "baseline",
                successes=20,
                latency_ms=Decimal("100"),
                cost_usd=Decimal("0.020"),
                quality_score=0.80,
            ),
            candidate=_metrics(
                "candidate",
                successes=17,
                failures=3,
                latency_ms=Decimal("70"),
                cost_usd=Decimal("0.010"),
                quality_score=0.90,
            ),
        ),
        policy=CanaryPromotionPolicy(
            min_baseline_samples=20,
            min_candidate_samples=20,
            rollback_success_rate_delta=-0.05,
        ),
    )

    assert decision.action is CanaryDecisionAction.ROLLBACK
    assert decision.reason == "success_rate_regression"
    assert "success_rate_delta:-0.150000" in decision.signals


@pytest.mark.anyio
async def test_validation_rejects_invalid_canary_inputs() -> None:
    call_provider = _caller({"baseline": "ok", "candidate": "ok"}, AttemptLog())

    invalid_requests = [
        CanaryProviderRequest(
            baseline_provider=_provider("baseline"),
            candidate_provider=_provider("candidate"),
            call_provider=call_provider,
            policy=CanaryRoutingPolicy(
                mode=CanaryMode.CANARY,
                candidate_fraction=-0.1,
                experiment_id="bad-fraction",
            ),
            traffic_key="request",
        ),
        CanaryProviderRequest(
            baseline_provider=_provider("same"),
            candidate_provider=_provider("same"),
            call_provider=call_provider,
            policy=CanaryRoutingPolicy(
                mode=CanaryMode.CANARY,
                candidate_fraction=0.1,
                experiment_id="duplicate",
            ),
            traffic_key="request",
        ),
        CanaryProviderRequest(
            baseline_provider=_provider("baseline"),
            candidate_provider=_provider("candidate"),
            call_provider=call_provider,
            policy=CanaryRoutingPolicy(
                mode=CanaryMode.CANARY,
                candidate_fraction=0.1,
                experiment_id="bad-bucket",
            ),
            traffic_key="request",
            traffic_bucket=1.0,
        ),
    ]

    for request in invalid_requests:
        with pytest.raises(ValueError):
            await route_with_canary(request)

    with pytest.raises(ValueError, match="quality_score"):
        CanaryObservation(provider_id="candidate", success=True, quality_score=1.1)
