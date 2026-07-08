"""Property-based tests for sub-budgets and chargeback.

Invariants:
    1. Sub-budget allocations always sum to ≤ total budget.
    2. Chargeback total_spend == sum(tag spend) + unallocated.
    3. Chargeback remaining_usd is non-negative and ≤ allocation.
    4. SubBudgetGate memory tracking is monotonic (non-decreasing) for admitted
       workloads.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pitwall.cost.budget_gate import BudgetAdmission, BudgetGate
from pitwall.cost.sub_budgets import (
    SubBudget,
    SubBudgetConfig,
    SubBudgetGate,
    SubBudgetRejected,
    generate_chargeback_report,
)

pytestmark = pytest.mark.property

_USD_QUANTUM = Decimal("0.000001")

# Strategies -----------------------------------------------------------------


decimal_usd = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
).map(lambda d: Decimal(str(d)))

positive_decimal_usd = st.decimals(
    min_value=Decimal("0.000001"),
    max_value=Decimal("100000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
).map(lambda d: Decimal(str(d)))

sub_budget_strategy = st.builds(
    SubBudget,
    tag=st.sampled_from(["ml", "infra", "team-a", "team-b", "inference"]),
    allocation_usd=decimal_usd,
)


def _has_duplicate_tags(budgets: list[SubBudget]) -> bool:
    tags = [budget.tag for budget in budgets]
    return len(tags) != len(set(tags))


# Tests ----------------------------------------------------------------------


@given(
    total=positive_decimal_usd,
    budgets=st.lists(sub_budget_strategy, min_size=0, max_size=10),
)
def test_allocation_sum_never_exceeds_total(total: Decimal, budgets: list[SubBudget]) -> None:
    total_allocation = sum(b.allocation_usd for b in budgets)
    if total_allocation > total or _has_duplicate_tags(budgets):
        with pytest.raises(ValueError, match="exceeds total|duplicate"):
            SubBudgetConfig(total_budget_usd=total, budgets=budgets)
    else:
        cfg = SubBudgetConfig(total_budget_usd=total, budgets=budgets)
        assert sum(b.allocation_usd for b in cfg.budgets) <= cfg.total_budget_usd
        assert len(cfg.tags()) == len(set(cfg.tags()))


@given(
    total=positive_decimal_usd,
    budgets=st.lists(sub_budget_strategy, min_size=0, max_size=10),
    unallocated_spend=decimal_usd,
)
def test_chargeback_total_equals_sum_of_parts(
    total: Decimal,
    budgets: list[SubBudget],
    unallocated_spend: Decimal,
) -> None:
    total_allocation = sum(b.allocation_usd for b in budgets)
    if total_allocation > total or _has_duplicate_tags(budgets):
        with pytest.raises(ValueError, match="exceeds total|duplicate"):
            SubBudgetConfig(total_budget_usd=total, budgets=budgets)
        return

    cfg = SubBudgetConfig(total_budget_usd=total, budgets=budgets)
    workloads: list[dict[str, Any]] = []
    for _ in range(3):
        workloads.append({"cost_estimate_usd": unallocated_spend})

    report = generate_chargeback_report(cfg, workloads)

    tag_sum = sum(li.spend_usd for li in report.line_items)
    computed_total = tag_sum + report.unallocated_spend_usd
    assert computed_total == report.total_spend_usd


@given(
    total=positive_decimal_usd,
    budgets=st.lists(sub_budget_strategy, min_size=1, max_size=5),
)
def test_chargeback_remaining_is_non_negative_and_lte_allocation(
    total: Decimal, budgets: list[SubBudget]
) -> None:
    total_allocation = sum(b.allocation_usd for b in budgets)
    if total_allocation > total or _has_duplicate_tags(budgets):
        with pytest.raises(ValueError, match="exceeds total|duplicate"):
            SubBudgetConfig(total_budget_usd=total, budgets=budgets)
        return

    cfg = SubBudgetConfig(total_budget_usd=total, budgets=budgets)
    workloads = [{"cost_estimate_usd": Decimal("0")}]
    report = generate_chargeback_report(cfg, workloads)

    for item in report.line_items:
        assert Decimal("0") <= item.remaining_usd <= item.allocation_usd


@given(
    allocation=positive_decimal_usd,
    estimates=st.lists(
        st.decimals(
            min_value=Decimal("0.000001"),
            max_value=Decimal("10"),
            allow_nan=False,
            allow_infinity=False,
            places=6,
        ).map(lambda d: Decimal(str(d))),
        min_size=1,
        max_size=20,
    ),
)
@settings(max_examples=30)
@pytest.mark.anyio
async def test_sub_budget_gate_memory_tracking_monotonic(
    allocation: Decimal, estimates: list[Decimal]
) -> None:
    budget_gate = MagicMock(spec=BudgetGate)
    budget_gate.monthly_budget_usd = Decimal("1000000")
    budget_gate.try_launch_admission = AsyncMock(
        side_effect=lambda **kwargs: BudgetAdmission(
            workload_id=f"wkl_{budget_gate.try_launch_admission.call_count}",
            is_new=True,
        )
    )

    cfg = SubBudgetConfig(
        total_budget_usd=allocation * 2,
        budgets=[SubBudget(tag="ml", allocation_usd=allocation)],
    )
    gate = SubBudgetGate(budget_gate, cfg)

    cumulative = Decimal("0")
    for estimate in estimates:
        if cumulative + estimate > allocation:
            with pytest.raises(SubBudgetRejected):
                await gate.try_launch(
                    tag="ml",
                    capability_id="cap_test",
                    provider_id="prov_test",
                    estimate_usd=estimate,
                )
            assert gate._memory_spend.get("ml", Decimal("0")) == cumulative
            break

        previous_spend = gate._memory_spend.get("ml", Decimal("0"))
        await gate.try_launch(
            tag="ml",
            capability_id="cap_test",
            provider_id="prov_test",
            estimate_usd=estimate,
        )
        cumulative += estimate
        assert gate._memory_spend["ml"] == previous_spend + estimate
        assert gate._memory_spend["ml"] == cumulative

    assert gate._memory_spend.get("ml", Decimal("0")) == cumulative


@given(total=positive_decimal_usd, count=st.integers(min_value=0, max_value=50))
def test_empty_workloads_produce_zero_chargeback(total: Decimal, count: int) -> None:
    cfg = SubBudgetConfig(
        total_budget_usd=total,
        budgets=[SubBudget(tag="x", allocation_usd=total / 2)],
    )
    workloads: list[dict[str, Any]] = [{} for _ in range(count)]
    report = generate_chargeback_report(cfg, workloads)
    assert report.total_spend_usd == Decimal("0")
    assert report.unallocated_spend_usd == Decimal("0")
    for item in report.line_items:
        assert item.spend_usd == Decimal("0")
        assert item.remaining_usd == item.allocation_usd
