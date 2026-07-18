"""Quality-aware routing for already-eligible provider/model candidates."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from pitwall.observability.scorecards import EntityScorecard


@dataclass(frozen=True, slots=True)
class QualityRoutingOption:
    """One provider/model candidate with scorecard quality, cost, and latency."""

    provider_id: str
    model_id: str
    quality: Decimal
    cost_usd: Decimal
    latency_ms: Decimal


@dataclass(frozen=True, slots=True)
class QualityRoutingPolicy:
    """Quality routing caps and weighted objective parameters."""

    max_cost_usd: Decimal | None = None
    max_latency_ms: Decimal | None = None
    quality_weight: Decimal = Decimal("1")
    cost_weight: Decimal = Decimal("0")
    latency_weight: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class QualityRoutingScore:
    """Weighted score for one quality routing candidate."""

    option: QualityRoutingOption
    policy: QualityRoutingPolicy
    objective: Decimal
    quality_component: Decimal
    cost_penalty: Decimal
    latency_penalty: Decimal

    @property
    def provider_id(self) -> str:
        """Provider id of the scored option."""

        return self.option.provider_id

    @property
    def model_id(self) -> str:
        """Model id of the scored option."""

        return self.option.model_id

    def to_dict(self) -> dict[str, str | None]:
        """Return a deterministic, JSON-ready score representation."""

        return {
            "provider_id": self.option.provider_id,
            "model_id": self.option.model_id,
            "quality": str(self.option.quality),
            "cost_usd": str(self.option.cost_usd),
            "latency_ms": str(self.option.latency_ms),
            "max_cost_usd": (
                str(self.policy.max_cost_usd) if self.policy.max_cost_usd is not None else None
            ),
            "max_latency_ms": (
                str(self.policy.max_latency_ms) if self.policy.max_latency_ms is not None else None
            ),
            "quality_weight": str(self.policy.quality_weight),
            "cost_weight": str(self.policy.cost_weight),
            "latency_weight": str(self.policy.latency_weight),
            "quality_component": str(self.quality_component),
            "cost_penalty": str(self.cost_penalty),
            "latency_penalty": str(self.latency_penalty),
            "objective": str(self.objective),
        }


def quality_option_from_scorecard(
    scorecard: EntityScorecard,
    *,
    model_id: str,
    use_normalized_quality: bool = True,
) -> QualityRoutingOption:
    """Build a routing candidate from a scorecard quality signal."""

    quality = scorecard.quality_normalized if use_normalized_quality else scorecard.quality
    return QualityRoutingOption(
        provider_id=scorecard.provider_id,
        model_id=model_id,
        quality=Decimal(str(quality)),
        cost_usd=scorecard.cost_usd,
        latency_ms=Decimal(str(scorecard.latency_ms)),
    )


def score_quality_routing_option(
    option: QualityRoutingOption,
    *,
    policy: QualityRoutingPolicy | None = None,
) -> QualityRoutingScore:
    """Return the weighted quality objective for one candidate."""

    normalized_policy = _validate_policy(policy or QualityRoutingPolicy())
    normalized_option = _validate_option(option)

    quality_component = normalized_policy.quality_weight * normalized_option.quality
    cost_penalty = normalized_policy.cost_weight * normalized_option.cost_usd
    latency_penalty = normalized_policy.latency_weight * normalized_option.latency_ms
    objective = quality_component - cost_penalty - latency_penalty

    return QualityRoutingScore(
        option=normalized_option,
        policy=normalized_policy,
        objective=objective,
        quality_component=quality_component,
        cost_penalty=cost_penalty,
        latency_penalty=latency_penalty,
    )


def sort_quality_routing_options(
    options: Iterable[QualityRoutingOption],
    *,
    policy: QualityRoutingPolicy | None = None,
) -> tuple[QualityRoutingScore, ...]:
    """Return policy-eligible candidates sorted by deterministic quality objective."""

    normalized_policy = _validate_policy(policy or QualityRoutingPolicy())
    scores = tuple(
        score
        for score in (
            score_quality_routing_option(option, policy=normalized_policy) for option in options
        )
        if _passes_constraints(score.option, normalized_policy)
    )
    return tuple(sorted(scores, key=_score_sort_key))


def select_quality_routing_option(
    options: Iterable[QualityRoutingOption],
    *,
    policy: QualityRoutingPolicy | None = None,
) -> QualityRoutingScore:
    """Select the highest-quality candidate satisfying policy constraints."""

    option_tuple = tuple(options)
    if not option_tuple:
        raise ValueError("options must contain at least one option")

    ranked = sort_quality_routing_options(option_tuple, policy=policy)
    if not ranked:
        raise ValueError("no options satisfy quality routing policy")
    return ranked[0]


def _passes_constraints(option: QualityRoutingOption, policy: QualityRoutingPolicy) -> bool:
    if policy.max_cost_usd is not None and option.cost_usd > policy.max_cost_usd:
        return False
    return not (policy.max_latency_ms is not None and option.latency_ms > policy.max_latency_ms)


def _score_sort_key(
    score: QualityRoutingScore,
) -> tuple[Decimal, Decimal, Decimal, Decimal, str, str]:
    return (
        -score.objective,
        -score.option.quality,
        score.option.cost_usd,
        score.option.latency_ms,
        score.option.provider_id,
        score.option.model_id,
    )


def _validate_option(option: QualityRoutingOption) -> QualityRoutingOption:
    return QualityRoutingOption(
        provider_id=_non_empty_string(option.provider_id, "provider_id"),
        model_id=_non_empty_string(option.model_id, "model_id"),
        quality=_quality_decimal(option.quality),
        cost_usd=_non_negative_decimal(option.cost_usd, "cost_usd"),
        latency_ms=_non_negative_decimal(option.latency_ms, "latency_ms"),
    )


def _validate_policy(policy: QualityRoutingPolicy) -> QualityRoutingPolicy:
    max_cost_usd = (
        _non_negative_decimal(policy.max_cost_usd, "max_cost_usd")
        if policy.max_cost_usd is not None
        else None
    )
    max_latency_ms = (
        _non_negative_decimal(policy.max_latency_ms, "max_latency_ms")
        if policy.max_latency_ms is not None
        else None
    )
    quality_weight = _non_negative_decimal(policy.quality_weight, "quality_weight")
    cost_weight = _non_negative_decimal(policy.cost_weight, "cost_weight")
    latency_weight = _non_negative_decimal(policy.latency_weight, "latency_weight")
    if quality_weight == 0 and cost_weight == 0 and latency_weight == 0:
        raise ValueError("at least one quality routing weight must be positive")

    return QualityRoutingPolicy(
        max_cost_usd=max_cost_usd,
        max_latency_ms=max_latency_ms,
        quality_weight=quality_weight,
        cost_weight=cost_weight,
        latency_weight=latency_weight,
    )


def _non_empty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{name} must not include surrounding whitespace")
    return value


def _quality_decimal(value: object) -> Decimal:
    quality = _non_negative_decimal(value, "quality")
    if quality > 1:
        raise ValueError("quality must be between 0 and 1")
    return quality


def _non_negative_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


__all__ = [
    "QualityRoutingOption",
    "QualityRoutingPolicy",
    "QualityRoutingScore",
    "quality_option_from_scorecard",
    "score_quality_routing_option",
    "select_quality_routing_option",
    "sort_quality_routing_options",
]
