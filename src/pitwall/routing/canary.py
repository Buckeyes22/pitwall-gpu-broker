"""Shadow/canary routing and promotion decisions.

This module is intentionally deterministic and I/O-free except for the
caller-supplied provider function.  It does not fetch providers, persist
metrics, or mutate routing configuration; callers feed observations back into
the promotion evaluator.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

type ProviderCallable[ProviderT, ResultT] = Callable[[ProviderT], Awaitable[ResultT]]
type ProviderIdCallable[ProviderT] = Callable[[ProviderT], str]
type ObservationResult = "CanaryObservation" | Awaitable["CanaryObservation"]
type ObservationCallable[ProviderT, ResultT] = Callable[[ProviderT, ResultT], ObservationResult]


class CanaryMode(StrEnum):
    """Traffic mode for a candidate provider."""

    SHADOW = "shadow"
    CANARY = "canary"


class CanaryDecisionAction(StrEnum):
    """Promotion decision for a candidate provider."""

    PROMOTE = "promote"
    HOLD = "hold"
    ROLLBACK = "rollback"


def _default_provider_id(provider: object) -> str:
    value = provider.get("id") if isinstance(provider, Mapping) else getattr(provider, "id", None)

    if not isinstance(value, str) or not value:
        raise ValueError("provider must include a non-empty id")
    return value


@dataclass(frozen=True, slots=True)
class CanaryRoutingPolicy:
    """Traffic allocation policy for one shadow/canary experiment."""

    mode: CanaryMode
    candidate_fraction: float
    experiment_id: str


@dataclass(frozen=True, slots=True)
class CanaryTrafficDecision:
    """Deterministic traffic-bucket decision for a request key."""

    mode: CanaryMode
    bucket: float
    candidate_fraction: float
    candidate_selected: bool


@dataclass(frozen=True, slots=True)
class CanaryObservation:
    """One provider observation collected by the canary controller."""

    provider_id: str
    success: bool
    latency_ms: Decimal | None = None
    cost_usd: Decimal | None = None
    quality_score: float | None = None

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("provider_id must be non-empty")
        if self.latency_ms is not None:
            object.__setattr__(
                self,
                "latency_ms",
                _decimal_non_negative(self.latency_ms, field_name="latency_ms"),
            )
        if self.cost_usd is not None:
            object.__setattr__(
                self,
                "cost_usd",
                _decimal_non_negative(self.cost_usd, field_name="cost_usd"),
            )
        if self.quality_score is not None:
            _validate_ratio(self.quality_score, field_name="quality_score")

    def to_dict(self) -> dict[str, bool | float | str | None]:
        return {
            "provider_id": self.provider_id,
            "success": self.success,
            "latency_ms": str(self.latency_ms) if self.latency_ms is not None else None,
            "cost_usd": str(self.cost_usd) if self.cost_usd is not None else None,
            "quality_score": self.quality_score,
        }


@dataclass(frozen=True, slots=True)
class ProviderMetrics:
    """Aggregated comparison metrics for one provider."""

    provider_id: str
    request_count: int = 0
    success_count: int = 0
    latency_ms_total: Decimal = Decimal("0")
    latency_sample_count: int = 0
    cost_usd_total: Decimal = Decimal("0")
    cost_sample_count: int = 0
    quality_score_total: float = 0.0
    quality_sample_count: int = 0

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("provider_id must be non-empty")
        _validate_non_negative_int(self.request_count, field_name="request_count")
        _validate_non_negative_int(self.success_count, field_name="success_count")
        _validate_non_negative_int(self.latency_sample_count, field_name="latency_sample_count")
        _validate_non_negative_int(self.cost_sample_count, field_name="cost_sample_count")
        _validate_non_negative_int(self.quality_sample_count, field_name="quality_sample_count")
        if self.success_count > self.request_count:
            raise ValueError("success_count must not exceed request_count")
        if self.latency_sample_count > self.request_count:
            raise ValueError("latency_sample_count must not exceed request_count")
        if self.cost_sample_count > self.request_count:
            raise ValueError("cost_sample_count must not exceed request_count")
        if self.quality_sample_count > self.request_count:
            raise ValueError("quality_sample_count must not exceed request_count")

        latency_total = _decimal_non_negative(self.latency_ms_total, field_name="latency_ms_total")
        cost_total = _decimal_non_negative(self.cost_usd_total, field_name="cost_usd_total")
        object.__setattr__(self, "latency_ms_total", latency_total)
        object.__setattr__(self, "cost_usd_total", cost_total)
        if not math.isfinite(self.quality_score_total) or self.quality_score_total < 0:
            raise ValueError("quality_score_total must be a finite non-negative number")

    @classmethod
    def from_observations(
        cls,
        provider_id: str,
        observations: Sequence[CanaryObservation],
    ) -> ProviderMetrics:
        latency_values = [
            observation.latency_ms
            for observation in observations
            if observation.latency_ms is not None
        ]
        cost_values = [
            observation.cost_usd for observation in observations if observation.cost_usd is not None
        ]
        quality_values = [
            observation.quality_score
            for observation in observations
            if observation.quality_score is not None
        ]
        for observation in observations:
            if observation.provider_id != provider_id:
                raise ValueError("observations must match provider_id")

        return cls(
            provider_id=provider_id,
            request_count=len(observations),
            success_count=sum(1 for observation in observations if observation.success),
            latency_ms_total=sum(latency_values, start=Decimal("0")),
            latency_sample_count=len(latency_values),
            cost_usd_total=sum(cost_values, start=Decimal("0")),
            cost_sample_count=len(cost_values),
            quality_score_total=sum(quality_values, start=0.0),
            quality_sample_count=len(quality_values),
        )

    @property
    def failure_count(self) -> int:
        return self.request_count - self.success_count

    @property
    def success_rate(self) -> float:
        if self.request_count == 0:
            return 0.0
        return self.success_count / self.request_count

    @property
    def error_rate(self) -> float:
        return 1.0 - self.success_rate

    @property
    def average_latency_ms(self) -> Decimal | None:
        if self.latency_sample_count == 0:
            return None
        return self.latency_ms_total / Decimal(self.latency_sample_count)

    @property
    def average_cost_usd(self) -> Decimal | None:
        if self.cost_sample_count == 0:
            return None
        return self.cost_usd_total / Decimal(self.cost_sample_count)

    @property
    def average_quality_score(self) -> float | None:
        if self.quality_sample_count == 0:
            return None
        return self.quality_score_total / self.quality_sample_count

    def to_dict(self) -> dict[str, float | int | str | None]:
        average_latency = self.average_latency_ms
        average_cost = self.average_cost_usd
        return {
            "provider_id": self.provider_id,
            "request_count": self.request_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": self.success_rate,
            "error_rate": self.error_rate,
            "average_latency_ms": str(average_latency) if average_latency is not None else None,
            "average_cost_usd": str(average_cost) if average_cost is not None else None,
            "average_quality_score": self.average_quality_score,
        }


@dataclass(frozen=True, slots=True)
class CanaryComparison:
    """Baseline and candidate metric windows for one decision."""

    baseline: ProviderMetrics
    candidate: ProviderMetrics

    def __post_init__(self) -> None:
        if self.baseline.provider_id == self.candidate.provider_id:
            raise ValueError("baseline and candidate provider ids must differ")


@dataclass(frozen=True, slots=True)
class CanaryPromotionPolicy:
    """Thresholds used to promote, hold, or roll back a candidate."""

    min_baseline_samples: int = 1
    min_candidate_samples: int = 1
    min_success_rate_delta: float = 0.0
    min_quality_delta: float = 0.0
    max_latency_ratio: float = 1.0
    max_cost_ratio: float = 1.0
    rollback_success_rate_delta: float = -0.05
    rollback_quality_delta: float = -0.05
    rollback_latency_ratio: float = 1.25
    rollback_cost_ratio: float = 1.25


_DEFAULT_PROMOTION_POLICY = CanaryPromotionPolicy()
_SHADOW_OBSERVATION_TASKS: set[asyncio.Task[CanaryObservation]] = set()


@dataclass(frozen=True, slots=True)
class CanaryPromotionDecision:
    """Auto-promotion decision plus comparison signals."""

    action: CanaryDecisionAction
    reason: str
    baseline: ProviderMetrics
    candidate: ProviderMetrics
    signals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "signals": list(self.signals),
        }


@dataclass(frozen=True, slots=True)
class CanaryProviderRequest[ProviderT, ResultT]:
    """Inputs for one shadow/canary provider call."""

    baseline_provider: ProviderT
    candidate_provider: ProviderT
    call_provider: ProviderCallable[ProviderT, ResultT]
    policy: CanaryRoutingPolicy
    traffic_key: str
    traffic_bucket: float | None = None
    observe_result: ObservationCallable[ProviderT, ResultT] | None = None
    provider_id: ProviderIdCallable[ProviderT] = _default_provider_id


@dataclass(frozen=True, slots=True)
class CanaryRoutingResult[ProviderT, ResultT]:
    """Served result plus shadow/canary routing metadata."""

    value: ResultT
    served_provider: ProviderT
    served_provider_id: str
    baseline_provider_id: str
    candidate_provider_id: str
    mode: CanaryMode
    candidate_selected: bool
    shadowed: bool
    traffic_bucket: float
    observations: tuple[CanaryObservation, ...]
    baseline_observation: CanaryObservation | None = None
    candidate_observation: CanaryObservation | None = None
    shadow_observation_task: asyncio.Task[CanaryObservation] | None = None


def stable_traffic_bucket(traffic_key: str, *, experiment_id: str) -> float:
    """Return a deterministic bucket in ``[0.0, 1.0)`` for traffic allocation."""

    digest = hashlib.sha256(f"{experiment_id}\0{traffic_key}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big") / float(1 << 64)


def select_canary_traffic(
    policy: CanaryRoutingPolicy,
    traffic_key: str,
    *,
    traffic_bucket: float | None = None,
) -> CanaryTrafficDecision:
    """Select whether this request should hit the candidate provider."""

    _validate_routing_policy(policy)
    bucket = (
        stable_traffic_bucket(traffic_key, experiment_id=policy.experiment_id)
        if traffic_bucket is None
        else traffic_bucket
    )
    _validate_bucket(bucket)
    return CanaryTrafficDecision(
        mode=policy.mode,
        bucket=bucket,
        candidate_fraction=policy.candidate_fraction,
        candidate_selected=bucket < policy.candidate_fraction,
    )


async def route_with_canary[ProviderT, ResultT](
    request: CanaryProviderRequest[ProviderT, ResultT],
) -> CanaryRoutingResult[ProviderT, ResultT]:
    """Route one request through a baseline/candidate experiment.

    Shadow mode always serves baseline output; the candidate is mirrored only
    when the traffic bucket falls under ``candidate_fraction``. Canary mode
    serves the candidate for selected buckets and the baseline otherwise.
    """

    baseline_id = request.provider_id(request.baseline_provider)
    candidate_id = request.provider_id(request.candidate_provider)
    if baseline_id == candidate_id:
        raise ValueError("baseline and candidate provider ids must differ")

    traffic = select_canary_traffic(
        request.policy,
        request.traffic_key,
        traffic_bucket=request.traffic_bucket,
    )
    if traffic.mode is CanaryMode.SHADOW:
        return await _route_shadow(
            request,
            baseline_id=baseline_id,
            candidate_id=candidate_id,
            traffic=traffic,
        )
    return await _route_canary(
        request,
        baseline_id=baseline_id,
        candidate_id=candidate_id,
        traffic=traffic,
    )


def evaluate_canary(
    comparison: CanaryComparison,
    *,
    policy: CanaryPromotionPolicy = _DEFAULT_PROMOTION_POLICY,
) -> CanaryPromotionDecision:
    """Return promote/hold/rollback for candidate metrics against baseline."""

    _validate_promotion_policy(policy)
    baseline = comparison.baseline
    candidate = comparison.candidate
    signals = _comparison_signals(baseline, candidate)

    if baseline.request_count < policy.min_baseline_samples:
        return _decision(
            CanaryDecisionAction.HOLD,
            "insufficient_baseline_samples",
            comparison,
            signals,
        )
    if candidate.request_count < policy.min_candidate_samples:
        return _decision(
            CanaryDecisionAction.HOLD,
            "insufficient_candidate_samples",
            comparison,
            signals,
        )

    success_delta = candidate.success_rate - baseline.success_rate
    if success_delta < policy.rollback_success_rate_delta:
        return _decision(
            CanaryDecisionAction.ROLLBACK,
            "success_rate_regression",
            comparison,
            signals,
        )

    quality_delta = _optional_delta(
        candidate.average_quality_score,
        baseline.average_quality_score,
    )
    if quality_delta is not None and quality_delta < policy.rollback_quality_delta:
        return _decision(
            CanaryDecisionAction.ROLLBACK,
            "quality_regression",
            comparison,
            signals,
        )

    latency_ratio = _optional_ratio(
        candidate.average_latency_ms,
        baseline.average_latency_ms,
    )
    if latency_ratio is not None and latency_ratio > policy.rollback_latency_ratio:
        return _decision(
            CanaryDecisionAction.ROLLBACK,
            "latency_regression",
            comparison,
            signals,
        )

    cost_ratio = _optional_ratio(candidate.average_cost_usd, baseline.average_cost_usd)
    if cost_ratio is not None and cost_ratio > policy.rollback_cost_ratio:
        return _decision(
            CanaryDecisionAction.ROLLBACK,
            "cost_regression",
            comparison,
            signals,
        )

    promotion_checks = [
        success_delta >= policy.min_success_rate_delta,
        quality_delta is None or quality_delta >= policy.min_quality_delta,
        latency_ratio is None or latency_ratio <= policy.max_latency_ratio,
        cost_ratio is None or cost_ratio <= policy.max_cost_ratio,
    ]
    wins = [
        success_delta > 0,
        quality_delta is not None and quality_delta > 0,
        latency_ratio is not None and latency_ratio < 1.0,
        cost_ratio is not None and cost_ratio < 1.0,
    ]
    if all(promotion_checks) and any(wins):
        return _decision(
            CanaryDecisionAction.PROMOTE,
            "candidate_beats_baseline",
            comparison,
            signals,
        )

    return _decision(
        CanaryDecisionAction.HOLD,
        "candidate_does_not_beat_baseline",
        comparison,
        signals,
    )


async def _route_shadow[ProviderT, ResultT](
    request: CanaryProviderRequest[ProviderT, ResultT],
    *,
    baseline_id: str,
    candidate_id: str,
    traffic: CanaryTrafficDecision,
) -> CanaryRoutingResult[ProviderT, ResultT]:
    baseline_value, baseline_observation = await _call_and_observe(
        request,
        request.baseline_provider,
        provider_id=baseline_id,
    )
    observations = [baseline_observation]
    shadow_observation_task: asyncio.Task[CanaryObservation] | None = None
    if traffic.candidate_selected:
        shadow_observation_task = _spawn_shadow_candidate_observation(
            request,
            request.candidate_provider,
            provider_id=candidate_id,
        )

    return CanaryRoutingResult(
        value=baseline_value,
        served_provider=request.baseline_provider,
        served_provider_id=baseline_id,
        baseline_provider_id=baseline_id,
        candidate_provider_id=candidate_id,
        mode=traffic.mode,
        candidate_selected=traffic.candidate_selected,
        shadowed=traffic.candidate_selected,
        traffic_bucket=traffic.bucket,
        observations=tuple(observations),
        baseline_observation=baseline_observation,
        candidate_observation=None,
        shadow_observation_task=shadow_observation_task,
    )


async def _route_canary[ProviderT, ResultT](
    request: CanaryProviderRequest[ProviderT, ResultT],
    *,
    baseline_id: str,
    candidate_id: str,
    traffic: CanaryTrafficDecision,
) -> CanaryRoutingResult[ProviderT, ResultT]:
    if traffic.candidate_selected:
        served_provider = request.candidate_provider
        served_provider_id = candidate_id
    else:
        served_provider = request.baseline_provider
        served_provider_id = baseline_id

    value, observation = await _call_and_observe(
        request,
        served_provider,
        provider_id=served_provider_id,
    )
    baseline_observation = observation if served_provider_id == baseline_id else None
    candidate_observation = observation if served_provider_id == candidate_id else None
    return CanaryRoutingResult(
        value=value,
        served_provider=served_provider,
        served_provider_id=served_provider_id,
        baseline_provider_id=baseline_id,
        candidate_provider_id=candidate_id,
        mode=traffic.mode,
        candidate_selected=traffic.candidate_selected,
        shadowed=False,
        traffic_bucket=traffic.bucket,
        observations=(observation,),
        baseline_observation=baseline_observation,
        candidate_observation=candidate_observation,
    )


async def _call_and_observe[ProviderT, ResultT](
    request: CanaryProviderRequest[ProviderT, ResultT],
    provider: ProviderT,
    *,
    provider_id: str,
) -> tuple[ResultT, CanaryObservation]:
    value = await request.call_provider(provider)
    observation = await _observation_for_success(request, provider, value, provider_id=provider_id)
    return value, observation


async def _call_shadow_candidate[ProviderT, ResultT](
    request: CanaryProviderRequest[ProviderT, ResultT],
    provider: ProviderT,
    *,
    provider_id: str,
) -> CanaryObservation:
    try:
        value = await request.call_provider(provider)
        return await _observation_for_success(request, provider, value, provider_id=provider_id)
    except Exception:  # reason: canary probe failure is an observation, never an exception
        return CanaryObservation(provider_id=provider_id, success=False)


def _spawn_shadow_candidate_observation[ProviderT, ResultT](
    request: CanaryProviderRequest[ProviderT, ResultT],
    provider: ProviderT,
    *,
    provider_id: str,
) -> asyncio.Task[CanaryObservation]:
    task = asyncio.create_task(
        _call_shadow_candidate(
            request,
            provider,
            provider_id=provider_id,
        )
    )
    _SHADOW_OBSERVATION_TASKS.add(task)
    task.add_done_callback(_discard_shadow_observation_task)
    return task


def _discard_shadow_observation_task(task: asyncio.Task[CanaryObservation]) -> None:
    _SHADOW_OBSERVATION_TASKS.discard(task)
    if not task.cancelled():
        task.exception()


async def _observation_for_success[ProviderT, ResultT](
    request: CanaryProviderRequest[ProviderT, ResultT],
    provider: ProviderT,
    value: ResultT,
    *,
    provider_id: str,
) -> CanaryObservation:
    if request.observe_result is None:
        return CanaryObservation(provider_id=provider_id, success=True)

    observation = request.observe_result(provider, value)
    if inspect.isawaitable(observation):
        resolved = await observation
    else:
        resolved = observation
    if not isinstance(resolved, CanaryObservation):
        raise ValueError("observe_result must return CanaryObservation")
    if resolved.provider_id != provider_id:
        raise ValueError("observation provider_id must match provider")
    return resolved


def _decision(
    action: CanaryDecisionAction,
    reason: str,
    comparison: CanaryComparison,
    signals: tuple[str, ...],
) -> CanaryPromotionDecision:
    return CanaryPromotionDecision(
        action=action,
        reason=reason,
        baseline=comparison.baseline,
        candidate=comparison.candidate,
        signals=signals,
    )


def _comparison_signals(
    baseline: ProviderMetrics,
    candidate: ProviderMetrics,
) -> tuple[str, ...]:
    signals = [
        f"success_rate_delta:{_format_signed(candidate.success_rate - baseline.success_rate)}"
    ]
    quality_delta = _optional_delta(
        candidate.average_quality_score,
        baseline.average_quality_score,
    )
    if quality_delta is not None:
        signals.append(f"quality_delta:{_format_signed(quality_delta)}")

    latency_ratio = _optional_ratio(
        candidate.average_latency_ms,
        baseline.average_latency_ms,
    )
    if latency_ratio is not None:
        signals.append(f"latency_ratio:{latency_ratio:.6f}")

    cost_ratio = _optional_ratio(candidate.average_cost_usd, baseline.average_cost_usd)
    if cost_ratio is not None:
        signals.append(f"cost_ratio:{cost_ratio:.6f}")

    return tuple(signals)


def _optional_delta(candidate: float | None, baseline: float | None) -> float | None:
    if candidate is None or baseline is None:
        return None
    return candidate - baseline


def _optional_ratio(candidate: Decimal | None, baseline: Decimal | None) -> float | None:
    if candidate is None or baseline is None:
        return None
    if baseline == 0:
        return 0.0 if candidate == 0 else math.inf
    return float(candidate / baseline)


def _format_signed(value: float) -> str:
    return f"{value:+.6f}"


def _validate_routing_policy(policy: CanaryRoutingPolicy) -> None:
    if not isinstance(policy.mode, CanaryMode):
        raise ValueError("mode must be a CanaryMode")
    _validate_fraction(policy.candidate_fraction, field_name="candidate_fraction")
    if not policy.experiment_id:
        raise ValueError("experiment_id must be non-empty")


def _validate_promotion_policy(policy: CanaryPromotionPolicy) -> None:
    _validate_positive_int(policy.min_baseline_samples, field_name="min_baseline_samples")
    _validate_positive_int(policy.min_candidate_samples, field_name="min_candidate_samples")
    _validate_finite(policy.min_success_rate_delta, field_name="min_success_rate_delta")
    _validate_finite(policy.min_quality_delta, field_name="min_quality_delta")
    _validate_finite_non_negative(policy.max_latency_ratio, field_name="max_latency_ratio")
    _validate_finite_non_negative(policy.max_cost_ratio, field_name="max_cost_ratio")
    _validate_finite(
        policy.rollback_success_rate_delta,
        field_name="rollback_success_rate_delta",
    )
    _validate_finite(policy.rollback_quality_delta, field_name="rollback_quality_delta")
    _validate_finite_non_negative(
        policy.rollback_latency_ratio,
        field_name="rollback_latency_ratio",
    )
    _validate_finite_non_negative(
        policy.rollback_cost_ratio,
        field_name="rollback_cost_ratio",
    )


def _validate_bucket(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0.0 or value >= 1.0:
        raise ValueError("traffic_bucket must be a finite number in [0, 1)")


def _validate_fraction(value: float, *, field_name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise ValueError(f"{field_name} must be a finite number between 0 and 1")


def _validate_ratio(value: float, *, field_name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise ValueError(f"{field_name} must be a finite number between 0 and 1")


def _validate_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _validate_non_negative_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _validate_finite(value: float, *, field_name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")


def _validate_finite_non_negative(value: float, *, field_name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0.0:
        raise ValueError(f"{field_name} must be finite and non-negative")


def _decimal_non_negative(value: Decimal, *, field_name: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} must be a finite non-negative decimal") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field_name} must be a finite non-negative decimal")
    return amount


__all__ = [
    "CanaryComparison",
    "CanaryDecisionAction",
    "CanaryMode",
    "CanaryObservation",
    "CanaryProviderRequest",
    "CanaryPromotionDecision",
    "CanaryPromotionPolicy",
    "CanaryRoutingPolicy",
    "CanaryRoutingResult",
    "CanaryTrafficDecision",
    "ProviderMetrics",
    "evaluate_canary",
    "route_with_canary",
    "select_canary_traffic",
    "stable_traffic_bucket",
]
