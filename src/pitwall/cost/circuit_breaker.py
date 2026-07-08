"""Budget circuit breaker with hysteresis for automatic downgrade or block."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

log = logging.getLogger("pitwall.cost.circuit_breaker")

type CircuitBreakerState = Literal["closed", "open", "half-open"]
type BreakerAction = Literal["allow", "downgrade", "block"]


@dataclass(frozen=True)
class CircuitBreakerDecision:
    """Decision emitted by the budget circuit breaker.

    Attributes:
        action: ``allow`` (normal flow), ``downgrade`` (cheaper provider/GPU),
            or ``block`` (reject new spend).
        reason: Human-readable explanation of the decision.
        state: The breaker's state *after* evaluation.
        headroom_usd: Absolute budget headroom at evaluation time.
        headroom_pct: Headroom as a percentage of the monthly budget.
        runway_hours: Estimated hours until budget exhaustion, or ``None``
            when no burn-rate data is provided.
    """

    action: BreakerAction
    reason: str
    state: CircuitBreakerState
    headroom_usd: Decimal
    headroom_pct: Decimal
    runway_hours: Decimal | None


@dataclass
class BudgetCircuitBreaker:
    """Stateful budget circuit breaker.

    Trips from ``closed`` → ``open`` when headroom or runway crosses below
    trip thresholds.  After *cooldown_seconds* elapses, transitions to
    ``half-open``.  Recovers to ``closed`` only when headroom/runway exceeds
    recovery thresholds (hysteresis).  Emits ``allow`` / ``downgrade`` / ``block``
    decisions that the gate can consult before admission.

    The breaker is deterministic: every :meth:`evaluate` call requires an
    explicit *now* timestamp, and identical inputs plus internal state always
    produce the same decision.
    """

    headroom_trip_pct: Decimal = Decimal("10.0")
    runway_trip_hours: Decimal = Decimal("24.0")
    recovery_headroom_pct: Decimal = Decimal("20.0")
    recovery_runway_hours: Decimal = Decimal("72.0")
    downgrade_headroom_pct: Decimal = Decimal("5.0")
    cooldown_seconds: float = 300.0

    _state: CircuitBreakerState = field(default="closed", init=False, repr=False)
    _last_trip_at: datetime | None = field(default=None, init=False, repr=False)

    def evaluate(
        self,
        *,
        budget_usd: Decimal,
        mtd_spend_usd: Decimal,
        now: datetime,
        burn_rate_usd_per_hour: Decimal | None = None,
    ) -> CircuitBreakerDecision:
        """Evaluate current budget state and emit a decision.

        Args:
            budget_usd: Monthly budget cap (must be non-negative).
            mtd_spend_usd: Month-to-date spend (must be non-negative).
            now: Explicit current timestamp.  Must include timezone info.
            burn_rate_usd_per_hour: Optional current burn rate.

        Returns:
            A :class:`CircuitBreakerDecision` carrying the recommended action
            and the breaker's post-evaluation state.
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include timezone information")

        now_utc = now.astimezone(UTC)
        headroom_usd = budget_usd - mtd_spend_usd
        if headroom_usd < Decimal("0"):
            headroom_usd = Decimal("0")

        headroom_pct = (
            (headroom_usd / budget_usd * Decimal("100")) if budget_usd > 0 else Decimal("0")
        )

        runway_hours: Decimal | None = None
        if burn_rate_usd_per_hour is not None and burn_rate_usd_per_hour > 0:
            runway_hours = headroom_usd / burn_rate_usd_per_hour

        previous_state = self._state
        new_state, action, reason = self._transition(
            headroom_pct=headroom_pct,
            runway_hours=runway_hours,
            now_utc=now_utc,
        )

        self._state = new_state
        if new_state == "open":
            if self._last_trip_at is None or previous_state == "half-open":
                self._last_trip_at = now_utc
        elif new_state == "closed":
            self._last_trip_at = None

        return CircuitBreakerDecision(
            action=action,
            reason=reason,
            state=self._state,
            headroom_usd=headroom_usd,
            headroom_pct=headroom_pct,
            runway_hours=runway_hours,
        )

    def _transition(
        self,
        *,
        headroom_pct: Decimal,
        runway_hours: Decimal | None,
        now_utc: datetime,
    ) -> tuple[CircuitBreakerState, BreakerAction, str]:
        if self._state == "closed":
            if self._should_trip(headroom_pct, runway_hours):
                return "open", *self._action_for_stress(headroom_pct, runway_hours)
            return "closed", "allow", "budget healthy"

        if self._state == "open":
            if self._last_trip_at is None:
                return "open", *self._action_for_stress(headroom_pct, runway_hours)
            elapsed = (now_utc - self._last_trip_at).total_seconds()
            if elapsed >= self.cooldown_seconds:
                if self._should_recover(headroom_pct, runway_hours):
                    return "closed", "allow", "budget recovered"
                return "half-open", *self._action_for_stress(headroom_pct, runway_hours)
            return "open", *self._action_for_stress(headroom_pct, runway_hours)

        # half-open
        if self._should_recover(headroom_pct, runway_hours):
            return "closed", "allow", "budget recovered"
        return "open", *self._action_for_stress(headroom_pct, runway_hours)

    def _should_trip(
        self,
        headroom_pct: Decimal,
        runway_hours: Decimal | None,
    ) -> bool:
        return headroom_pct <= self.headroom_trip_pct or (
            runway_hours is not None and runway_hours <= self.runway_trip_hours
        )

    def _should_recover(
        self,
        headroom_pct: Decimal,
        runway_hours: Decimal | None,
    ) -> bool:
        return headroom_pct >= self.recovery_headroom_pct and (
            runway_hours is None or runway_hours >= self.recovery_runway_hours
        )

    def _action_for_stress(
        self,
        headroom_pct: Decimal,
        runway_hours: Decimal | None,
    ) -> tuple[BreakerAction, str]:
        if headroom_pct <= self.downgrade_headroom_pct:
            return "block", "budget exhausted"
        if runway_hours is not None and runway_hours <= self.runway_trip_hours:
            return "downgrade", "burn rate critical"
        return "downgrade", "headroom low"

    @property
    def state(self) -> CircuitBreakerState:
        """Current breaker state."""
        return self._state

    def reset(self) -> None:
        """Reset the breaker to ``closed``.  Useful for testing."""
        self._state = "closed"
        self._last_trip_at = None


__all__ = [
    "BreakerAction",
    "BudgetCircuitBreaker",
    "CircuitBreakerDecision",
    "CircuitBreakerState",
]
