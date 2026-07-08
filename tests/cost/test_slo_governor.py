"""Hermetic unit tests for the cost SLO governor."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pitwall.cost.circuit_breaker import (
    BudgetCircuitBreaker,
    CircuitBreakerDecision,
)
from pitwall.cost.slo_governor import (
    CostGovernor,
    CostSLO,
    GovernorDecision,
)

_NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)


def _slo(**kwargs: object) -> CostSLO:
    defaults: dict[str, object] = {
        "per_day_target_usd": Decimal("100.00"),
        "per_request_p95_usd": None,
        "throttle_threshold": Decimal("0.80"),
        "defer_threshold": Decimal("1.00"),
    }
    defaults.update(kwargs)
    return CostSLO(**defaults)  # type: ignore[arg-type]  # reason: kwargs dict loses precise types; constructor validates


def _evaluate(
    governor: CostGovernor,
    *,
    slo: CostSLO | None = None,
    burn_rate_usd_per_day: Decimal = Decimal("50.00"),
    now: datetime = _NOW,
    breaker_decision: CircuitBreakerDecision | None = None,
    recent_request_costs_usd: list[Decimal] | None = None,
) -> GovernorDecision:
    return governor.evaluate(
        slo=slo or _slo(),
        burn_rate_usd_per_day=burn_rate_usd_per_day,
        now=now,
        breaker_decision=breaker_decision,
        recent_request_costs_usd=recent_request_costs_usd,
    )


# ── Basic velocity-based pacing ──────────────────────────────────────


def test_allow_when_velocity_within_target() -> None:
    governor = CostGovernor()
    decision = _evaluate(governor, burn_rate_usd_per_day=Decimal("50.00"))

    assert decision.action == "allow"
    assert decision.reason == "burn rate within daily SLO target"
    assert decision.velocity_ratio == Decimal("0.5")


def test_throttle_when_velocity_approaches_target() -> None:
    governor = CostGovernor()
    decision = _evaluate(governor, burn_rate_usd_per_day=Decimal("80.00"))

    assert decision.action == "throttle"
    assert "approaching" in decision.reason
    assert decision.velocity_ratio == Decimal("0.8")


def test_defer_when_velocity_exceeds_target() -> None:
    governor = CostGovernor()
    decision = _evaluate(governor, burn_rate_usd_per_day=Decimal("100.00"))

    assert decision.action == "defer"
    assert "daily SLO target" in decision.reason
    assert decision.velocity_ratio == Decimal("1")


# ── Breaker composition ──────────────────────────────────────────────


def test_breaker_block_overrides_allow() -> None:
    governor = CostGovernor()
    breaker = BudgetCircuitBreaker()
    breaker_decision = breaker.evaluate(
        budget_usd=Decimal("1000.00"),
        mtd_spend_usd=Decimal("1000.00"),
        now=_NOW,
    )
    assert breaker_decision.action == "block"

    decision = _evaluate(
        governor,
        burn_rate_usd_per_day=Decimal("50.00"),
        breaker_decision=breaker_decision,
    )

    assert decision.action == "defer"
    assert decision.reason == "breaker: budget exhausted"


def test_breaker_downgrade_escalates_allow_to_throttle() -> None:
    governor = CostGovernor()
    breaker = BudgetCircuitBreaker()
    # 9 % headroom: below trip (10 %) but above downgrade (5 %)
    breaker_decision = breaker.evaluate(
        budget_usd=Decimal("1000.00"),
        mtd_spend_usd=Decimal("910.00"),
        now=_NOW,
    )
    assert breaker_decision.action == "downgrade"

    decision = _evaluate(
        governor,
        burn_rate_usd_per_day=Decimal("50.00"),
        breaker_decision=breaker_decision,
    )

    assert decision.action == "throttle"
    assert decision.reason == "breaker: budget stressed"


def test_breaker_downgrade_does_not_weaken_defer() -> None:
    governor = CostGovernor()
    breaker = BudgetCircuitBreaker()
    # 9 % headroom triggers downgrade, not block
    breaker_decision = breaker.evaluate(
        budget_usd=Decimal("1000.00"),
        mtd_spend_usd=Decimal("910.00"),
        now=_NOW,
    )

    decision = _evaluate(
        governor,
        burn_rate_usd_per_day=Decimal("120.00"),
        breaker_decision=breaker_decision,
    )

    assert decision.action == "defer"
    assert "daily SLO target" in decision.reason


# ── Per-request p95 ──────────────────────────────────────────────────


def test_request_p95_triggers_throttle() -> None:
    governor = CostGovernor()
    decision = _evaluate(
        governor,
        slo=_slo(per_request_p95_usd=Decimal("10.00")),
        burn_rate_usd_per_day=Decimal("50.00"),
        recent_request_costs_usd=[Decimal("8.50")],
    )

    assert decision.action == "throttle"
    assert "request p95" in decision.reason


def test_request_p95_triggers_defer() -> None:
    governor = CostGovernor()
    decision = _evaluate(
        governor,
        slo=_slo(per_request_p95_usd=Decimal("10.00")),
        burn_rate_usd_per_day=Decimal("50.00"),
        recent_request_costs_usd=[Decimal("12.00")],
    )

    assert decision.action == "defer"
    assert "request p95" in decision.reason


def test_request_p95_ignored_when_no_slo() -> None:
    governor = CostGovernor()
    decision = _evaluate(
        governor,
        burn_rate_usd_per_day=Decimal("50.00"),
        recent_request_costs_usd=[Decimal("100.00")],
    )

    assert decision.action == "allow"


# ── Determinism / explicit now ───────────────────────────────────────


def test_requires_timezone_aware_now() -> None:
    governor = CostGovernor()
    naive = datetime(2026, 6, 2, 12, 0, 0)

    with pytest.raises(ValueError, match="timezone"):
        governor.evaluate(
            slo=_slo(),
            burn_rate_usd_per_day=Decimal("50.00"),
            now=naive,
        )


# ── Edge cases ───────────────────────────────────────────────────────


def test_zero_burn_rate_always_allows() -> None:
    governor = CostGovernor()
    decision = _evaluate(governor, burn_rate_usd_per_day=Decimal("0.00"))

    assert decision.action == "allow"
    assert decision.velocity_ratio == Decimal("0")


def test_negative_target_raises() -> None:
    with pytest.raises(ValueError):
        CostSLO(per_day_target_usd=Decimal("-1.00"))


# ── Custom thresholds ────────────────────────────────────────────────


def test_custom_thresholds() -> None:
    governor = CostGovernor()
    slo = _slo(
        throttle_threshold=Decimal("0.50"),
        defer_threshold=Decimal("0.90"),
    )

    decision_allow = _evaluate(governor, slo=slo, burn_rate_usd_per_day=Decimal("40.00"))
    assert decision_allow.action == "allow"

    decision_throttle = _evaluate(governor, slo=slo, burn_rate_usd_per_day=Decimal("60.00"))
    assert decision_throttle.action == "throttle"

    decision_defer = _evaluate(governor, slo=slo, burn_rate_usd_per_day=Decimal("95.00"))
    assert decision_defer.action == "defer"


# ── Validation ───────────────────────────────────────────────────────


def test_invalid_threshold_order_raises() -> None:
    with pytest.raises(ValueError):
        CostSLO(
            per_day_target_usd=Decimal("100.00"),
            throttle_threshold=Decimal("1.00"),
            defer_threshold=Decimal("0.80"),
        )


def test_equal_thresholds_raise() -> None:
    with pytest.raises(ValueError):
        CostSLO(
            per_day_target_usd=Decimal("100.00"),
            throttle_threshold=Decimal("0.80"),
            defer_threshold=Decimal("0.80"),
        )


# ── Decision immutability ────────────────────────────────────────────


def test_decision_is_frozen() -> None:
    decision = GovernorDecision(
        action="allow",
        reason="ok",
        velocity_ratio=Decimal("0.5"),
        request_p95_ratio=None,
        slo=_slo(),
        breaker_action=None,
    )

    with pytest.raises(AttributeError):
        decision.action = "defer"  # type: ignore[misc]  # reason: frozen dataclass: assignment intentionally rejected
