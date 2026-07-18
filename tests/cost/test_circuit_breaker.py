"""Hermetic unit tests for the budget circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pitwall.cost.circuit_breaker import (
    BudgetCircuitBreaker,
    CircuitBreakerDecision,
)

_NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)


def _breaker(**kwargs: object) -> BudgetCircuitBreaker:
    defaults: dict[str, object] = {
        "headroom_trip_pct": Decimal("10.0"),
        "runway_trip_hours": Decimal("24.0"),
        "recovery_headroom_pct": Decimal("20.0"),
        "recovery_runway_hours": Decimal("72.0"),
        "downgrade_headroom_pct": Decimal("5.0"),
        "cooldown_seconds": 300.0,
    }
    defaults.update(kwargs)
    return BudgetCircuitBreaker(**defaults)


def _evaluate(
    breaker: BudgetCircuitBreaker,
    *,
    budget_usd: Decimal = Decimal("1000.00"),
    mtd_spend_usd: Decimal = Decimal("0.00"),
    now: datetime = _NOW,
    burn_rate_usd_per_hour: Decimal | None = None,
) -> CircuitBreakerDecision:
    return breaker.evaluate(
        budget_usd=budget_usd,
        mtd_spend_usd=mtd_spend_usd,
        now=now,
        burn_rate_usd_per_hour=burn_rate_usd_per_hour,
    )


# ── Basic state transitions ──────────────────────────────────────────


def test_closed_allows_healthy_budget() -> None:
    breaker = _breaker()
    decision = _evaluate(breaker, mtd_spend_usd=Decimal("100.00"))

    assert decision.action == "allow"
    assert decision.state == "closed"
    assert decision.reason == "budget healthy"
    assert decision.headroom_usd == Decimal("900.00")
    assert decision.headroom_pct == Decimal("90.0")


def test_closed_trips_on_low_headroom() -> None:
    breaker = _breaker()
    # 10 % headroom exactly at trip threshold
    decision = _evaluate(breaker, mtd_spend_usd=Decimal("900.00"))

    assert decision.action == "downgrade"
    assert decision.state == "open"
    assert decision.headroom_pct == Decimal("10.0")


def test_closed_trips_on_low_runway() -> None:
    breaker = _breaker()
    # $500 headroom, $25/hr burn = 20 h runway (< 24 h trip)
    decision = _evaluate(
        breaker,
        mtd_spend_usd=Decimal("500.00"),
        burn_rate_usd_per_hour=Decimal("25.00"),
    )

    assert decision.action == "downgrade"
    assert decision.state == "open"
    assert decision.reason == "burn rate critical"
    assert decision.runway_hours == Decimal("20.0")


def test_open_stays_open_during_cooldown() -> None:
    breaker = _breaker()
    _evaluate(breaker, mtd_spend_usd=Decimal("900.00"))
    assert breaker.state == "open"

    decision = _evaluate(
        breaker,
        mtd_spend_usd=Decimal("900.00"),
        now=_NOW + timedelta(seconds=100),
    )

    assert decision.state == "open"
    assert decision.action == "downgrade"


def test_open_transitions_to_half_open_after_cooldown() -> None:
    breaker = _breaker()
    _evaluate(breaker, mtd_spend_usd=Decimal("900.00"))

    decision = _evaluate(
        breaker,
        mtd_spend_usd=Decimal("900.00"),
        now=_NOW + timedelta(seconds=300),
    )

    assert decision.state == "half-open"
    assert decision.action == "downgrade"


def test_half_open_recovers_when_healthy() -> None:
    breaker = _breaker()
    # Trip it
    _evaluate(breaker, mtd_spend_usd=Decimal("900.00"))
    # Move to half-open after cooldown
    _evaluate(
        breaker,
        mtd_spend_usd=Decimal("900.00"),
        now=_NOW + timedelta(seconds=300),
    )
    assert breaker.state == "half-open"

    # Recovery: headroom back to 25 % (> 20 % recovery)
    decision = _evaluate(
        breaker,
        mtd_spend_usd=Decimal("750.00"),
        now=_NOW + timedelta(seconds=301),
    )

    assert decision.state == "closed"
    assert decision.action == "allow"
    assert decision.reason == "budget recovered"


def test_half_open_returns_to_open_when_still_stressed() -> None:
    breaker = _breaker()
    _evaluate(breaker, mtd_spend_usd=Decimal("900.00"))
    _evaluate(
        breaker,
        mtd_spend_usd=Decimal("900.00"),
        now=_NOW + timedelta(seconds=300),
    )
    assert breaker.state == "half-open"

    decision = _evaluate(
        breaker,
        mtd_spend_usd=Decimal("900.00"),
        now=_NOW + timedelta(seconds=301),
    )

    assert decision.state == "open"
    assert decision.action == "downgrade"


def test_failed_half_open_probe_restarts_cooldown() -> None:
    breaker = _breaker()
    _evaluate(breaker, mtd_spend_usd=Decimal("900.00"), now=_NOW)
    _evaluate(
        breaker,
        mtd_spend_usd=Decimal("900.00"),
        now=_NOW + timedelta(seconds=300),
    )
    assert breaker.state == "half-open"

    failed_probe = _evaluate(
        breaker,
        mtd_spend_usd=Decimal("900.00"),
        now=_NOW + timedelta(seconds=301),
    )
    assert failed_probe.state == "open"

    before_second_cooldown = _evaluate(
        breaker,
        mtd_spend_usd=Decimal("900.00"),
        now=_NOW + timedelta(seconds=302),
    )

    assert before_second_cooldown.state == "open"
    assert before_second_cooldown.action == "downgrade"


# ── Hysteresis ───────────────────────────────────────────────────────


def test_hysteresis_prevents_immediate_reclose() -> None:
    breaker = _breaker()
    # Trip at exactly 10 %
    _evaluate(breaker, mtd_spend_usd=Decimal("900.00"))
    assert breaker.state == "open"

    # 15 % headroom is above trip (10 %) but below recovery (20 %).
    # After cooldown the breaker enters half-open first.
    decision = _evaluate(
        breaker,
        mtd_spend_usd=Decimal("850.00"),
        now=_NOW + timedelta(seconds=301),
    )

    assert decision.state == "half-open"
    assert decision.action == "downgrade"

    # A second evaluation from half-open with no recovery bounces back to open.
    decision2 = _evaluate(
        breaker,
        mtd_spend_usd=Decimal("850.00"),
        now=_NOW + timedelta(seconds=302),
    )
    assert decision2.state == "open"


# ── Downgrade vs block ───────────────────────────────────────────────


def test_downgrade_when_headroom_above_downgrade_threshold() -> None:
    breaker = _breaker()
    # 7 % headroom: below trip (10 %) but above downgrade (5 %)
    decision = _evaluate(breaker, mtd_spend_usd=Decimal("930.00"))

    assert decision.action == "downgrade"
    assert decision.reason == "headroom low"


def test_block_when_headroom_at_or_below_downgrade_threshold() -> None:
    breaker = _breaker()
    # Exactly 5 % headroom
    decision = _evaluate(breaker, mtd_spend_usd=Decimal("950.00"))

    assert decision.action == "block"
    assert decision.reason == "budget exhausted"


def test_block_when_budget_exhausted() -> None:
    breaker = _breaker()
    decision = _evaluate(breaker, mtd_spend_usd=Decimal("1000.00"))

    assert decision.action == "block"
    assert decision.headroom_usd == Decimal("0")
    assert decision.headroom_pct == Decimal("0")


# ── Runway edge cases ────────────────────────────────────────────────


def test_no_runway_when_burn_rate_is_zero() -> None:
    breaker = _breaker()
    decision = _evaluate(
        breaker,
        burn_rate_usd_per_hour=Decimal("0.00"),
    )

    assert decision.runway_hours is None
    assert decision.action == "allow"


def test_no_runway_when_burn_rate_is_none() -> None:
    breaker = _breaker()
    decision = _evaluate(breaker)

    assert decision.runway_hours is None
    assert decision.action == "allow"


def test_negative_headroom_clamped_to_zero() -> None:
    breaker = _breaker()
    decision = _evaluate(breaker, mtd_spend_usd=Decimal("1200.00"))

    assert decision.headroom_usd == Decimal("0")
    assert decision.headroom_pct == Decimal("0")
    assert decision.action == "block"


# ── Determinism / explicit now ───────────────────────────────────────


def test_same_inputs_produce_same_decision() -> None:
    breaker = _breaker()
    d1 = _evaluate(breaker, mtd_spend_usd=Decimal("900.00"))
    breaker.reset()
    d2 = _evaluate(breaker, mtd_spend_usd=Decimal("900.00"))

    assert d1 == d2


def test_requires_timezone_aware_now() -> None:
    breaker = _breaker()
    naive = datetime(2026, 6, 2, 12, 0, 0)

    with pytest.raises(ValueError, match="timezone"):
        breaker.evaluate(
            budget_usd=Decimal("1000.00"),
            mtd_spend_usd=Decimal("0.00"),
            now=naive,
        )


# ── Reset ────────────────────────────────────────────────────────────


def test_reset_returns_to_closed() -> None:
    breaker = _breaker()
    _evaluate(breaker, mtd_spend_usd=Decimal("900.00"))
    assert breaker.state == "open"

    breaker.reset()

    assert breaker.state == "closed"
    decision = _evaluate(breaker, mtd_spend_usd=Decimal("0.00"))
    assert decision.action == "allow"


# ── Zero budget edge case ────────────────────────────────────────────


def test_zero_budget_always_blocks() -> None:
    breaker = _breaker()
    decision = _evaluate(breaker, budget_usd=Decimal("0.00"))

    assert decision.headroom_pct == Decimal("0")
    assert decision.action == "block"


# ── Runway recovery hysteresis ───────────────────────────────────────


def test_runway_recovery_requires_higher_threshold() -> None:
    breaker = _breaker(
        headroom_trip_pct=Decimal("50.0"),
        recovery_headroom_pct=Decimal("60.0"),
        runway_trip_hours=Decimal("10.0"),
        recovery_runway_hours=Decimal("20.0"),
    )
    # Trip on runway: $100 headroom, $20/hr = 5 h (< 10 h trip)
    _evaluate(
        breaker,
        budget_usd=Decimal("1000.00"),
        mtd_spend_usd=Decimal("900.00"),
        burn_rate_usd_per_hour=Decimal("20.00"),
    )
    assert breaker.state == "open"

    # After cooldown, runway = 15 h (> 10 h trip but < 20 h recovery).
    # Breaker enters half-open first.
    decision = _evaluate(
        breaker,
        budget_usd=Decimal("1000.00"),
        mtd_spend_usd=Decimal("900.00"),
        burn_rate_usd_per_hour=Decimal("6.67"),
        now=_NOW + timedelta(seconds=301),
    )

    assert decision.state == "half-open"
    assert decision.action == "downgrade"

    # Second call from half-open with no recovery returns to open.
    decision2 = _evaluate(
        breaker,
        budget_usd=Decimal("1000.00"),
        mtd_spend_usd=Decimal("900.00"),
        burn_rate_usd_per_hour=Decimal("6.67"),
        now=_NOW + timedelta(seconds=302),
    )
    assert decision2.state == "open"


# ── Decision immutability ────────────────────────────────────────────


def test_decision_is_frozen() -> None:
    decision = CircuitBreakerDecision(
        action="allow",
        reason="ok",
        state="closed",
        headroom_usd=Decimal("1.00"),
        headroom_pct=Decimal("10.0"),
        runway_hours=None,
    )

    with pytest.raises(AttributeError):
        decision.action = "block"
