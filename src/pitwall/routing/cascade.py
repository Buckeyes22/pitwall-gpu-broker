"""Complexity-based cascade routing.

Cascade routing runs an ordered cheap-to-expensive tier chain sequentially.
Each provider output is evaluated by a caller-supplied quality gate; the first
passing output wins, and failed gate attempts are retained with cost metadata.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

type ProviderCallable[ProviderT, ResultT] = Callable[[ProviderT], Awaitable[ResultT]]
type ProviderIdCallable[ProviderT] = Callable[[ProviderT], str]
type CostCallable[ProviderT, ResultT] = Callable[[ProviderT, ResultT], Decimal]


def _default_provider_id(provider: object) -> str:
    value = provider.get("id") if isinstance(provider, Mapping) else getattr(provider, "id", None)

    if not isinstance(value, str) or not value:
        raise ValueError("provider must include a non-empty id")
    return value


@dataclass(frozen=True, slots=True)
class CascadeGateDecision:
    """Result from a quality/confidence gate for one model output."""

    passed: bool
    confidence: float | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise ValueError("passed must be a bool")
        if self.confidence is not None and (
            isinstance(self.confidence, bool)
            or not 0.0 <= self.confidence <= 1.0
            or self.confidence != self.confidence
        ):
            raise ValueError("confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, bool | float | str | None]:
        return {
            "passed": self.passed,
            "confidence": self.confidence,
            "reason": self.reason,
        }


type QualityGateResult = CascadeGateDecision | Awaitable[CascadeGateDecision]
type QualityGateCallable[ProviderT, ResultT] = Callable[[ProviderT, ResultT], QualityGateResult]


@dataclass(frozen=True, slots=True)
class CascadeTier[ProviderT]:
    """One ordered provider tier with its expected per-attempt cost."""

    provider: ProviderT
    estimated_cost_usd: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "estimated_cost_usd",
            _decimal_usd(self.estimated_cost_usd, field_name="estimated_cost_usd"),
        )


@dataclass(frozen=True, slots=True)
class CascadeAttempt[ProviderT, ResultT]:
    """One executed cascade tier and its gate/cost outcome."""

    provider_id: str
    provider: ProviderT
    attempt: int
    value: ResultT
    gate: CascadeGateDecision
    cost_usd: Decimal

    def __post_init__(self) -> None:
        if self.provider_id == "":
            raise ValueError("provider_id must be non-empty")
        if isinstance(self.attempt, bool) or self.attempt < 1:
            raise ValueError("attempt must be a positive integer")
        object.__setattr__(self, "cost_usd", _decimal_usd(self.cost_usd, field_name="cost_usd"))

    @property
    def gate_passed(self) -> bool:
        return self.gate.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "attempt": self.attempt,
            "gate": self.gate.to_dict(),
            "cost_usd": str(self.cost_usd),
        }


@dataclass(frozen=True, slots=True)
class CascadeProviderRequest[ProviderT, ResultT]:
    """Inputs for one sequential cascade execution."""

    tiers: Sequence[CascadeTier[ProviderT]]
    call_provider: ProviderCallable[ProviderT, ResultT]
    quality_gate: QualityGateCallable[ProviderT, ResultT]
    max_attempts: int | None = None
    provider_id: ProviderIdCallable[ProviderT] = _default_provider_id
    cost_of_result: CostCallable[ProviderT, ResultT] | None = None


@dataclass(frozen=True, slots=True)
class CascadeProviderResult[ProviderT, ResultT]:
    """Winning model output and complete attempted cascade metadata."""

    value: ResultT
    provider: ProviderT
    provider_id: str
    attempts: tuple[CascadeAttempt[ProviderT, ResultT], ...]
    total_cost_usd: Decimal

    @property
    def attempted_provider_ids(self) -> tuple[str, ...]:
        return tuple(attempt.provider_id for attempt in self.attempts)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def escalated(self) -> bool:
        return self.attempt_count > 1


class CascadeRoutingError(RuntimeError):
    """Raised when no attempted tier passes the quality gate."""

    def __init__(
        self,
        message: str,
        *,
        attempts: Sequence[CascadeAttempt[Any, Any]],
    ) -> None:
        super().__init__(message)
        self.attempts = tuple(attempts)
        self.attempted_provider_ids = tuple(attempt.provider_id for attempt in self.attempts)
        self.total_cost_usd = _total_cost(self.attempts)


async def route_with_cascade[ProviderT, ResultT](
    request: CascadeProviderRequest[ProviderT, ResultT],
) -> CascadeProviderResult[ProviderT, ResultT]:
    """Run providers sequentially until the quality gate passes.

    Tiers must be pre-ordered from cheapest to most expensive by
    ``estimated_cost_usd``. Gate failures are not provider failures: the model
    was called, its cost is counted, and execution moves to the next tier.
    """

    selected_tiers, provider_ids = _validate_request(request)
    attempts: list[CascadeAttempt[ProviderT, ResultT]] = []

    for index, tier in enumerate(selected_tiers, start=1):
        provider = tier.provider
        provider_id = provider_ids[index - 1]
        value = await request.call_provider(provider)
        gate = await _gate_decision(request.quality_gate(provider, value))
        cost_usd = _attempt_cost(request, provider, value, tier=tier)
        attempt = CascadeAttempt(
            provider_id=provider_id,
            provider=provider,
            attempt=index,
            value=value,
            gate=gate,
            cost_usd=cost_usd,
        )
        attempts.append(attempt)
        if gate.passed is True:
            return CascadeProviderResult(
                value=value,
                provider=provider,
                provider_id=provider_id,
                attempts=tuple(attempts),
                total_cost_usd=_total_cost(attempts),
            )

    raise CascadeRoutingError(
        "cascade routing exhausted all attempted tiers without passing quality gate",
        attempts=attempts,
    )


def _validate_request[ProviderT, ResultT](
    request: CascadeProviderRequest[ProviderT, ResultT],
) -> tuple[tuple[CascadeTier[ProviderT], ...], tuple[str, ...]]:
    tiers = tuple(request.tiers)
    if not tiers:
        raise ValueError("tiers must contain at least one tier")

    _validate_tier_order(tiers)
    max_attempts = _max_attempts(request.max_attempts, tier_count=len(tiers))
    provider_ids = tuple(request.provider_id(tier.provider) for tier in tiers)
    _validate_provider_ids(provider_ids)
    return tiers[:max_attempts], provider_ids[:max_attempts]


def _validate_tier_order[ProviderT](tiers: Sequence[CascadeTier[ProviderT]]) -> None:
    previous_cost: Decimal | None = None
    for tier in tiers:
        cost = tier.estimated_cost_usd
        if previous_cost is not None and cost < previous_cost:
            raise ValueError("tiers must be ordered by non-decreasing estimated_cost_usd")
        previous_cost = cost


def _max_attempts(value: int | None, *, tier_count: int) -> int:
    if value is None:
        return tier_count
    if isinstance(value, bool) or value < 1:
        raise ValueError("max_attempts must be a positive integer")
    return min(value, tier_count)


def _validate_provider_ids(provider_ids: tuple[str, ...]) -> None:
    seen: set[str] = set()
    for provider_id in provider_ids:
        if provider_id == "":
            raise ValueError("provider ids must be non-empty strings")
        if provider_id in seen:
            raise ValueError(f"provider id {provider_id!r} is duplicated")
        seen.add(provider_id)


async def _gate_decision(outcome: QualityGateResult) -> CascadeGateDecision:
    if inspect.isawaitable(outcome):
        decision = await outcome
    else:
        decision = outcome
    if not isinstance(decision, CascadeGateDecision):
        raise ValueError("quality_gate must return CascadeGateDecision")
    return decision


def _attempt_cost[ProviderT, ResultT](
    request: CascadeProviderRequest[ProviderT, ResultT],
    provider: ProviderT,
    value: ResultT,
    *,
    tier: CascadeTier[ProviderT],
) -> Decimal:
    if request.cost_of_result is None:
        return tier.estimated_cost_usd
    return _decimal_usd(
        request.cost_of_result(provider, value),
        field_name="cost_of_result",
    )


def _decimal_usd(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, Decimal):
        amount = value
    else:
        raise ValueError(f"{field_name} must be a Decimal")

    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field_name} must be a finite non-negative USD amount")
    return amount


def _total_cost(attempts: Sequence[CascadeAttempt[Any, Any]]) -> Decimal:
    return sum((attempt.cost_usd for attempt in attempts), start=Decimal("0"))


__all__ = [
    "CascadeAttempt",
    "CascadeGateDecision",
    "CascadeProviderRequest",
    "CascadeProviderResult",
    "CascadeRoutingError",
    "CascadeTier",
    "route_with_cascade",
]
