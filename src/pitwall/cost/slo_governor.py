"""Cost SLO governor — pace admission based on spend velocity vs SLO targets."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from pitwall.cost.circuit_breaker import BreakerAction, CircuitBreakerDecision

log = logging.getLogger("pitwall.cost.slo_governor")

_USD_QUANTUM = Decimal("0.000001")

type PacingAction = Literal["allow", "throttle", "defer"]


class CostSLO(BaseModel):
    """Cost Service-Level Objective defining daily and per-request targets.

    The governor compares current *burn_rate_usd_per_day* against
    *per_day_target_usd* and emits ``allow``, ``throttle``, or ``defer``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    per_day_target_usd: Decimal
    per_request_p95_usd: Decimal | None = None
    throttle_threshold: Decimal = Decimal("0.80")
    defer_threshold: Decimal = Decimal("1.00")

    @model_validator(mode="after")
    def _validate(self) -> CostSLO:
        if self.per_day_target_usd <= 0:
            raise ValueError("per_day_target_usd must be positive")
        if self.per_request_p95_usd is not None and self.per_request_p95_usd <= 0:
            raise ValueError("per_request_p95_usd must be positive")
        if self.throttle_threshold >= self.defer_threshold:
            raise ValueError("throttle_threshold must be strictly less than defer_threshold")
        if self.throttle_threshold < Decimal("0"):
            raise ValueError("throttle_threshold must be non-negative")
        if self.defer_threshold < Decimal("0"):
            raise ValueError("defer_threshold must be non-negative")
        return self


@dataclass(frozen=True)
class GovernorDecision:
    """Pacing decision emitted by the cost SLO governor.

    Attributes:
        action: ``allow`` (normal flow), ``throttle`` (reduce throughput),
            or ``defer`` (reject new spend).
        reason: Human-readable explanation of the decision.
        velocity_ratio: ``burn_rate_usd_per_day / per_day_target_usd``.
        request_p95_ratio: ``p95_request_cost / per_request_p95_usd``, or
            ``None`` when no per-request SLO or no recent cost data.
        slo: The :class:`CostSLO` that was evaluated.
        breaker_action: The breaker's action at evaluation time, if one was
            supplied; ``None`` otherwise.
    """

    action: PacingAction
    reason: str
    velocity_ratio: Decimal
    request_p95_ratio: Decimal | None
    slo: CostSLO
    breaker_action: BreakerAction | None


class CostGovernor:
    """Deterministic cost SLO governor.

    Evaluates spend velocity against a :class:`CostSLO` and emits a pacing
    decision.  The governor is intentionally *compositional*: it accepts an
    optional :class:`CircuitBreakerDecision` and factors the breaker's
    absolute-budget signal into its own velocity-based pacing logic without
    duplicating headroom/runway calculations.

    The governor is deterministic: every :meth:`evaluate` call requires an
    explicit *now* timestamp, and identical inputs always produce the same
    :class:`GovernorDecision`.
    """

    def evaluate(
        self,
        *,
        slo: CostSLO,
        burn_rate_usd_per_day: Decimal,
        now: datetime,
        breaker_decision: CircuitBreakerDecision | None = None,
        recent_request_costs_usd: Sequence[Decimal] | None = None,
    ) -> GovernorDecision:
        """Evaluate current spend velocity against *slo* and emit a decision.

        Args:
            slo: Cost SLO targets to compare against.
            burn_rate_usd_per_day: Current daily burn rate (e.g. from
                :class:`BurnRateForecaster`).
            now: Explicit current timestamp.  Must include timezone info.
            breaker_decision: Optional breaker decision to compose with.
            recent_request_costs_usd: Optional sequence of recent request
                costs for per-request p95 analysis.

        Returns:
            A :class:`GovernorDecision` carrying the recommended pacing action
            and computed ratios.
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include timezone information")

        _ = now.astimezone(UTC)

        velocity_ratio = _ratio(burn_rate_usd_per_day, slo.per_day_target_usd)

        request_p95_ratio: Decimal | None = None
        if recent_request_costs_usd and slo.per_request_p95_usd is not None:
            p95 = _percentile(sorted(recent_request_costs_usd), 95)
            request_p95_ratio = _ratio(p95, slo.per_request_p95_usd)

        breaker_action = breaker_decision.action if breaker_decision is not None else None

        action, reason = self._decide(
            slo=slo,
            velocity_ratio=velocity_ratio,
            request_p95_ratio=request_p95_ratio,
            breaker_action=breaker_action,
        )

        return GovernorDecision(
            action=action,
            reason=reason,
            velocity_ratio=velocity_ratio,
            request_p95_ratio=request_p95_ratio,
            slo=slo,
            breaker_action=breaker_action,
        )

    def _decide(
        self,
        *,
        slo: CostSLO,
        velocity_ratio: Decimal,
        request_p95_ratio: Decimal | None,
        breaker_action: BreakerAction | None,
    ) -> tuple[PacingAction, str]:
        # Velocity-based baseline
        if velocity_ratio >= slo.defer_threshold:
            action: PacingAction = "defer"
            reason = f"burn rate {velocity_ratio:.2f}x daily SLO target"
        elif velocity_ratio >= slo.throttle_threshold:
            action = "throttle"
            reason = f"burn rate {velocity_ratio:.2f}x approaching daily SLO target"
        else:
            action = "allow"
            reason = "burn rate within daily SLO target"

        # Per-request p95 can escalate
        if request_p95_ratio is not None:
            if request_p95_ratio >= slo.defer_threshold:
                action = "defer"
                reason = f"request p95 {request_p95_ratio:.2f}x SLO target"
            elif request_p95_ratio >= slo.throttle_threshold and action == "allow":
                action = "throttle"
                reason = f"request p95 {request_p95_ratio:.2f}x approaching SLO target"

        # Breaker composes: absolute budget protection takes precedence
        if breaker_action == "block":
            action = "defer"
            reason = "breaker: budget exhausted"
        elif breaker_action == "downgrade":
            if action == "allow":
                action = "throttle"
                reason = "breaker: budget stressed"
            # If already throttle or defer, keep the stronger signal

        return action, reason


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return (numerator / denominator).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)


def _percentile(sorted_values: Sequence[Decimal], pct: int) -> Decimal:
    if not sorted_values:
        return Decimal("0")
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    rank = (Decimal(pct) * n / Decimal("100")).to_integral_value(rounding=ROUND_CEILING)
    idx = int(rank) - 1
    if idx < 0:
        idx = 0
    if idx >= n:
        idx = n - 1
    return sorted_values[idx]


__all__ = [
    "CostGovernor",
    "CostSLO",
    "GovernorDecision",
    "PacingAction",
]
