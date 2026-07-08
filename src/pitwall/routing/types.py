"""Routing types for the 4-stage provider selection algorithm."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EliminationReason(StrEnum):
    """Reasons a provider is eliminated during Stage 1 hard-constraint filtering.

    Ordered to match the spec §7 Stage 1 description.
    """

    CAPABILITY_MISMATCH = "capability_mismatch"
    REGION_MISMATCH = "region_mismatch"
    CUDA_MISMATCH = "cuda_mismatch"
    GPU_CLASS_MISMATCH = "gpu_class_mismatch"
    PAYLOAD_TOO_LARGE = "payload_too_large"


class ProviderEliminated(StrEnum):
    """Reasons a provider is eliminated during routing."""

    CAPABILITY_MISMATCH = "capability_mismatch"
    REGION_MISMATCH = "region_mismatch"
    CUDA_MISMATCH = "cuda_mismatch"
    GPU_CLASS_MISMATCH = "gpu_class_mismatch"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    DISABLED = "disabled"
    HEALTH_UNHEALTHY = "health_unhealthy"
    HEALTH_COOLDOWN = "health_cooldown"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"


@dataclass(frozen=True, slots=True)
class CapacityProbeKey:
    """Cache key used for Stage 4 RunPod pod-lease capacity checks."""

    datacenter: str
    gpu_name: str
    cloud_type: str
    gpu_count: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "datacenter": self.datacenter,
            "gpu_name": self.gpu_name,
            "cloud_type": self.cloud_type,
            "gpu_count": self.gpu_count,
        }


@dataclass(frozen=True, slots=True)
class CapacityDecision:
    """Stage 4 capacity-probe decision for one pod-lease provider candidate."""

    provider_id: str
    available: bool | None
    reason: str
    keys: tuple[CapacityProbeKey, ...] = field(default_factory=tuple)
    selected_key: CapacityProbeKey | None = None
    stage: int = 4

    @property
    def checked(self) -> bool:
        return bool(self.keys)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "stage": self.stage,
            "checked": self.checked,
            "available": self.available,
            "reason": self.reason,
            "keys": [key.to_dict() for key in self.keys],
            "selected_key": self.selected_key.to_dict() if self.selected_key else None,
        }


@dataclass(frozen=True, slots=True)
class Hints:
    """Consumer hints used to rank providers in Stage 3 scoring.

    Corresponds to the Hints type used in score_provider (§7 spec).
    """

    latency_sensitive: bool = False
    cost_sensitive: bool = False
    region_preference: str | None = None


@dataclass(frozen=True, slots=True)
class ObservedMetrics:
    """Observed runtime metrics for providers, used in Stage 3 scoring."""

    recent_error_rate: float = 0.0


@dataclass(frozen=True, slots=True)
class ScoreExplanation:
    """Deterministic breakdown for the Stage 3 score formula."""

    provider_id: str
    base_score: float = 100.0
    latency_penalty: float = 0.0
    warm_worker_bonus: float = 0.0
    cost_penalty: float = 0.0
    region_bonus: float = 0.0
    recent_error_penalty: float = 0.0
    priority_multiplier: float = 1.0
    score_before_multiplier: float = 100.0
    final_score: float = 100.0

    @property
    def score(self) -> float:
        """Final score after priority multiplier."""

        return self.final_score

    @property
    def error_penalty(self) -> float:
        """Backward-compatible alias for recent-error score penalty."""

        return self.recent_error_penalty

    def to_dict(self) -> dict[str, float | str]:
        return {
            "provider_id": self.provider_id,
            "base_score": self.base_score,
            "latency_penalty": self.latency_penalty,
            "warm_worker_bonus": self.warm_worker_bonus,
            "cost_penalty": self.cost_penalty,
            "region_bonus": self.region_bonus,
            "recent_error_penalty": self.recent_error_penalty,
            "priority_multiplier": self.priority_multiplier,
            "score_before_multiplier": self.score_before_multiplier,
            "final_score": self.final_score,
        }


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    """Input to the routing algorithm — what the consumer is asking for."""

    capability_name: str
    payload_bytes: int | None = None
    required_gpu_class: str | None = None
    required_region: str | None = None
    required_volume_id: str | None = None
    hints: Hints | None = None
    capability_id: str | None = None
    required_cuda_min: str | None = None
    required_cuda_version: str | None = None
    stream: bool = False

    @property
    def payload_mb(self) -> float | None:
        """Payload size in megabytes, or None if unknown."""
        if self.payload_bytes is None:
            return None
        return self.payload_bytes / (1024 * 1024)


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    """Result of Stage 1 hard-constraint evaluation on a single provider."""

    provider_id: str
    passed: bool
    reason: EliminationReason | None = None
    reasons: tuple[EliminationReason, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        normalized = tuple(self.reasons)
        reason = self.reason

        if self.passed:
            reason = None
            normalized = ()
        elif reason is None and normalized:
            reason = normalized[0]
        elif reason is not None and not normalized:
            normalized = (reason,)

        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "reasons", normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "passed": self.passed,
            "reason": self.reason.value if self.reason else None,
            "reasons": [reason.value for reason in self.reasons],
        }


@dataclass(frozen=True, slots=True)
class RouteElimination:
    """Provider dropped during route planning."""

    provider_id: str
    stage: int
    reason: ProviderEliminated
    reasons: tuple[ProviderEliminated, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        normalized = tuple(self.reasons) or (self.reason,)
        object.__setattr__(self, "reasons", normalized)
        object.__setattr__(self, "reason", normalized[0])

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "stage": self.stage,
            "reason": self.reason.value,
            "reasons": [reason.value for reason in self.reasons],
        }


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    """Stage 3 ranked provider candidate."""

    provider_id: str
    provider: object
    rank: int
    score: float
    score_explanation: ScoreExplanation
    fallback_for: tuple[str, ...] = field(default_factory=tuple)
    explicit_fallback_chain: tuple[str, ...] = field(default_factory=tuple)

    @property
    def score_breakdown(self) -> ScoreExplanation:
        """Alias used by callers that name the explanation a breakdown."""

        return self.score_explanation

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "rank": self.rank,
            "score": self.score,
            "fallback_for": list(self.fallback_for),
            "explicit_fallback_chain": list(self.explicit_fallback_chain),
            "score_explanation": self.score_explanation.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RouteAttempt:
    """One planned provider attempt with retry/backoff metadata."""

    provider_id: str
    provider: object
    attempt: int
    score: float
    score_explanation: ScoreExplanation
    backoff_before_attempt_s: float = 0.0

    @property
    def attempt_number(self) -> int:
        return self.attempt

    @property
    def backoff_s(self) -> float:
        return self.backoff_before_attempt_s

    @property
    def score_breakdown(self) -> ScoreExplanation:
        return self.score_explanation

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "provider_id": self.provider_id,
            "score": self.score,
            "backoff_before_attempt_s": self.backoff_before_attempt_s,
            "score_explanation": self.score_explanation.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """Complete deterministic output of the pure routing planner."""

    request: RoutingRequest
    attempts: tuple[RouteAttempt, ...] = field(default_factory=tuple)
    ranked_candidates: tuple[RouteCandidate, ...] = field(default_factory=tuple)
    eliminated: tuple[RouteElimination, ...] = field(default_factory=tuple)
    capacity_decisions: tuple[CapacityDecision, ...] = field(default_factory=tuple)
    max_attempts: int = 3

    @property
    def selected_provider_id(self) -> str | None:
        if not self.attempts:
            return None
        return self.attempts[0].provider_id

    @property
    def selected_provider(self) -> object | None:
        if not self.attempts:
            return None
        return self.attempts[0].provider

    @property
    def fallback_chain(self) -> tuple[str, ...]:
        """Provider ids in the order execution should attempt them."""

        return tuple(attempt.provider_id for attempt in self.attempts)

    @property
    def fallback_provider_ids(self) -> tuple[str, ...]:
        return self.fallback_chain[1:]

    @property
    def fallback_providers(self) -> tuple[object, ...]:
        return tuple(attempt.provider for attempt in self.attempts[1:])

    @property
    def score_breakdown(self) -> dict[str, dict[str, float | str]]:
        return {
            candidate.provider_id: candidate.score_explanation.to_dict()
            for candidate in self.ranked_candidates
        }

    @property
    def score_explanations(self) -> dict[str, ScoreExplanation]:
        return {
            candidate.provider_id: candidate.score_explanation
            for candidate in self.ranked_candidates
        }

    @property
    def dropped_provider_reasons(self) -> dict[str, list[str]]:
        return {
            item.provider_id: [reason.value for reason in item.reasons] for item in self.eliminated
        }

    @property
    def capacity_decisions_by_provider(self) -> dict[str, CapacityDecision]:
        return {decision.provider_id: decision for decision in self.capacity_decisions}

    @property
    def capacity_probe_decisions(self) -> tuple[CapacityDecision, ...]:
        return self.capacity_decisions

    @property
    def stage4_decisions(self) -> tuple[CapacityDecision, ...]:
        return self.capacity_decisions

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_provider_id": self.selected_provider_id,
            "fallback_chain": list(self.fallback_chain),
            "fallback_provider_ids": list(self.fallback_provider_ids),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "ranked_candidates": [candidate.to_dict() for candidate in self.ranked_candidates],
            "eliminated": [item.to_dict() for item in self.eliminated],
            "dropped_provider_reasons": self.dropped_provider_reasons,
            "capacity_decisions": [decision.to_dict() for decision in self.capacity_decisions],
            "max_attempts": self.max_attempts,
        }


def parse_stream_from_bytes(body: bytes) -> bool:
    """Parse stream=true from request bytes.

    Inspects the JSON body for a ``stream`` key set to ``true``
    (boolean or string ``"true"``).  Returns ``False`` if the body
    is empty, not valid JSON, or does not contain ``stream``.

    This function is used for routing decisions only — the original
    bytes are always forwarded unchanged to the upstream provider.
    """
    if not body:
        return False
    try:
        data = __import__("json").loads(body)
    except Exception:  # reason: malformed body means not-JSON; return False
        return False
    if not isinstance(data, dict):
        return False
    stream_value = data.get("stream")
    if stream_value is True:
        return True
    return bool(isinstance(stream_value, str) and stream_value.lower() == "true")


__all__ = [
    "CapacityDecision",
    "CapacityProbeKey",
    "ConstraintResult",
    "EliminationReason",
    "Hints",
    "ObservedMetrics",
    "ProviderEliminated",
    "RouteAttempt",
    "RouteCandidate",
    "RouteElimination",
    "RoutePlan",
    "RoutingRequest",
    "ScoreExplanation",
    "parse_stream_from_bytes",
]
