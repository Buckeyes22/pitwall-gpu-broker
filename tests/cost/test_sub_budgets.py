"""Unit tests for blast-radius sub-budgets and chargeback.

Hermetic — no live network or database.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.cost.budget_gate import BudgetAdmission, BudgetRejected, BudgetSnapshot
from pitwall.cost.sub_budgets import (
    SubBudget,
    SubBudgetConfig,
    SubBudgetGate,
    SubBudgetRejected,
    SubBudgetSnapshot,
    generate_chargeback_report,
)

# ---------------------------------------------------------------------------
# SubBudget / SubBudgetConfig
# ---------------------------------------------------------------------------


def test_sub_budget_creation() -> None:
    b = SubBudget(tag="ml", allocation_usd=Decimal("100.000000"))
    assert b.tag == "ml"
    assert b.allocation_usd == Decimal("100.000000")
    assert b.description is None


def test_sub_budget_with_description() -> None:
    b = SubBudget(tag="team-a", allocation_usd=Decimal("50"), description="Team A")
    assert b.description == "Team A"


def test_sub_budget_rejects_negative_allocation() -> None:
    with pytest.raises(ValueError, match="allocation_usd.*non-negative"):
        SubBudget(tag="x", allocation_usd=Decimal("-1"))


def test_sub_budget_config_creation() -> None:
    cfg = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[
            SubBudget(tag="ml", allocation_usd=Decimal("400")),
            SubBudget(tag="infra", allocation_usd=Decimal("300")),
        ],
    )
    assert cfg.total_budget_usd == Decimal("1000")
    assert cfg.allocation_for("ml") == Decimal("400")
    assert cfg.allocation_for("infra") == Decimal("300")
    assert cfg.allocation_for("unknown") == Decimal("0")
    assert cfg.tags() == ["ml", "infra"]


def test_sub_budget_config_rejects_excessive_allocations() -> None:
    with pytest.raises(ValueError, match="exceeds total"):
        SubBudgetConfig(
            total_budget_usd=Decimal("500"),
            budgets=[SubBudget(tag="a", allocation_usd=Decimal("600"))],
        )


def test_sub_budget_config_rejects_duplicate_tags() -> None:
    with pytest.raises(ValueError, match="duplicate.*ml"):
        SubBudgetConfig(
            total_budget_usd=Decimal("1000"),
            budgets=[
                SubBudget(tag="ml", allocation_usd=Decimal("400")),
                SubBudget(tag="ml", allocation_usd=Decimal("100")),
            ],
        )


def test_sub_budget_config_allows_exact_total() -> None:
    cfg = SubBudgetConfig(
        total_budget_usd=Decimal("500"),
        budgets=[SubBudget(tag="a", allocation_usd=Decimal("500"))],
    )
    assert cfg.allocation_for("a") == Decimal("500")


def test_sub_budget_config_rejects_non_positive_total() -> None:
    with pytest.raises(ValueError, match="total_budget_usd.*positive"):
        SubBudgetConfig(total_budget_usd=Decimal("0"))
    with pytest.raises(ValueError, match="total_budget_usd.*positive"):
        SubBudgetConfig(total_budget_usd=Decimal("-10"))


def test_sub_budget_config_empty_budgets_ok() -> None:
    cfg = SubBudgetConfig(total_budget_usd=Decimal("1000"))
    assert cfg.budgets == []
    assert cfg.tags() == []


# ---------------------------------------------------------------------------
# SubBudgetSnapshot / SubBudgetRejected
# ---------------------------------------------------------------------------


def test_sub_budget_snapshot_serializable() -> None:
    snap = SubBudgetSnapshot(
        tag="ml",
        tag_allocation_usd=Decimal("100.000000"),
        tag_mtd_spend_usd=Decimal("75.000000"),
        tag_estimate_usd=Decimal("30.000000"),
        tag_remaining_usd=Decimal("25.000000"),
    )
    d = snap.to_serializable_dict()
    assert d == {
        "tag": "ml",
        "tag_allocation_usd": "100.000000",
        "tag_mtd_spend_usd": "75.000000",
        "tag_estimate_usd": "30.000000",
        "tag_remaining_usd": "25.000000",
    }
    json.loads(snap.model_dump_json())


def test_sub_budget_rejected_response_body() -> None:
    snap = SubBudgetSnapshot(
        tag="ml",
        tag_allocation_usd=Decimal("100"),
        tag_mtd_spend_usd=Decimal("80"),
        tag_estimate_usd=Decimal("30"),
        tag_remaining_usd=Decimal("20"),
    )
    exc = SubBudgetRejected("sub_budget", snap)
    assert exc.status_code == 402
    assert exc.error_code == "sub_budget_rejected"
    body = exc.to_response_body()
    assert body["error"] == "sub_budget_rejected"
    assert body["reason"] == "sub_budget"
    assert body["snapshot"]["tag"] == "ml"
    assert exc.to_http_response_body() == body


# ---------------------------------------------------------------------------
# SubBudgetGate
# ---------------------------------------------------------------------------


def _mock_budget_gate(
    *, monthly: Decimal = Decimal("1000"), admitted_id: str = "wkl_001"
) -> MagicMock:
    gate = MagicMock(spec="pitwall.cost.budget_gate.BudgetGate")
    gate.monthly_budget_usd = monthly
    gate.try_launch = AsyncMock(return_value=admitted_id)
    gate.try_launch_admission = AsyncMock(
        return_value=BudgetAdmission(workload_id=admitted_id, is_new=True)
    )
    gate.current_mtd_spend = AsyncMock(return_value=Decimal("0"))
    return gate


@pytest.mark.asyncio
async def test_sub_budget_gate_admits_within_allocation() -> None:
    budget_gate = _mock_budget_gate()
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("100"))],
    )
    gate = SubBudgetGate(budget_gate, config)

    workload_id = await gate.try_launch(
        tag="ml",
        capability_id="cap_test",
        provider_id="prov_test",
        estimate_usd=Decimal("50"),
    )

    assert workload_id == "wkl_001"
    budget_gate.try_launch_admission.assert_awaited_once()
    budget_gate.try_launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_sub_budget_gate_rejects_unknown_tag() -> None:
    budget_gate = _mock_budget_gate()
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("100"))],
    )
    gate = SubBudgetGate(budget_gate, config)

    with pytest.raises(SubBudgetRejected) as exc_info:
        await gate.try_launch(
            tag="unknown",
            capability_id="cap_test",
            provider_id="prov_test",
            estimate_usd=Decimal("10"),
        )

    assert exc_info.value.reason == "unknown_tag"
    assert exc_info.value.snapshot.tag == "unknown"
    assert exc_info.value.snapshot.tag_allocation_usd == Decimal("0")
    budget_gate.try_launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_sub_budget_gate_rejects_when_sub_budget_exceeded() -> None:
    budget_gate = _mock_budget_gate()
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("100"))],
    )
    gate = SubBudgetGate(
        budget_gate,
        config,
        tag_mtd_spend=lambda _tag: AsyncMock(return_value=Decimal("80"))(),
    )

    with pytest.raises(SubBudgetRejected) as exc_info:
        await gate.try_launch(
            tag="ml",
            capability_id="cap_test",
            provider_id="prov_test",
            estimate_usd=Decimal("30"),
        )

    assert exc_info.value.reason == "sub_budget"
    assert exc_info.value.snapshot.tag_mtd_spend_usd == Decimal("80")
    assert exc_info.value.snapshot.tag_estimate_usd == Decimal("30")
    assert exc_info.value.snapshot.tag_remaining_usd == Decimal("20")
    budget_gate.try_launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_sub_budget_gate_allows_exact_boundary() -> None:
    budget_gate = _mock_budget_gate()
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("100"))],
    )
    gate = SubBudgetGate(
        budget_gate,
        config,
        tag_mtd_spend=lambda _tag: AsyncMock(return_value=Decimal("50"))(),
    )

    workload_id = await gate.try_launch(
        tag="ml",
        capability_id="cap_test",
        provider_id="prov_test",
        estimate_usd=Decimal("50"),
    )

    assert workload_id == "wkl_001"


@pytest.mark.asyncio
async def test_sub_budget_gate_propagates_budget_rejected() -> None:
    budget_gate = _mock_budget_gate()
    budget_gate.try_launch_admission = AsyncMock(
        side_effect=BudgetRejected(
            "monthly_budget",
            BudgetSnapshot(
                monthly_budget_usd=Decimal("1000"),
                per_request_max_usd=Decimal("100"),
                mtd_spend_usd=Decimal("950"),
                estimate_usd=Decimal("100"),
                budget_remaining_usd=Decimal("50"),
            ),
        )
    )
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("500"))],
    )
    gate = SubBudgetGate(budget_gate, config)

    with pytest.raises(BudgetRejected):
        await gate.try_launch(
            tag="ml",
            capability_id="cap_test",
            provider_id="prov_test",
            estimate_usd=Decimal("100"),
        )


@pytest.mark.asyncio
async def test_sub_budget_gate_rejects_non_positive_estimate() -> None:
    budget_gate = _mock_budget_gate()
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("100"))],
    )
    gate = SubBudgetGate(budget_gate, config)

    with pytest.raises(ValueError, match="estimate_usd.*positive"):
        await gate.try_launch(
            tag="ml",
            capability_id="cap_test",
            provider_id="prov_test",
            estimate_usd=Decimal("0"),
        )


@pytest.mark.asyncio
async def test_sub_budget_gate_memory_tracking_increments() -> None:
    budget_gate = _mock_budget_gate(admitted_id="wkl_001")
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("100"))],
    )
    gate = SubBudgetGate(budget_gate, config)

    await gate.try_launch(
        tag="ml",
        capability_id="cap_test",
        provider_id="prov_test",
        estimate_usd=Decimal("30"),
    )
    await gate.try_launch(
        tag="ml",
        capability_id="cap_test",
        provider_id="prov_test",
        estimate_usd=Decimal("20"),
    )

    # Third launch should now fail because 30 + 20 + 60 > 100
    with pytest.raises(SubBudgetRejected) as exc_info:
        await gate.try_launch(
            tag="ml",
            capability_id="cap_test",
            provider_id="prov_test",
            estimate_usd=Decimal("60"),
        )

    assert exc_info.value.snapshot.tag_mtd_spend_usd == Decimal("50")
    assert exc_info.value.snapshot.tag_estimate_usd == Decimal("60")


@pytest.mark.asyncio
async def test_sub_budget_gate_passes_through_idempotency_key() -> None:
    budget_gate = _mock_budget_gate()
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("100"))],
    )
    gate = SubBudgetGate(budget_gate, config)

    await gate.try_launch(
        tag="ml",
        capability_id="cap_test",
        provider_id="prov_test",
        estimate_usd=Decimal("10"),
        idempotency_key="idem_1",
        submitted_at=None,
        workload_type="training",
    )

    call_kwargs = budget_gate.try_launch_admission.await_args.kwargs
    assert call_kwargs["idempotency_key"] == "idem_1"
    assert call_kwargs["workload_type"] == "training"


@pytest.mark.asyncio
async def test_sub_budget_gate_idempotency_replay_bypasses_unknown_tag_rejection() -> None:
    budget_gate = _mock_budget_gate(admitted_id="wkl_existing")
    budget_gate.try_launch_admission = AsyncMock(
        return_value=BudgetAdmission(workload_id="wkl_existing", is_new=False)
    )
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("100"))],
    )
    gate = SubBudgetGate(budget_gate, config)

    workload_id = await gate.try_launch(
        tag="unknown",
        capability_id="cap_test",
        provider_id="prov_test",
        estimate_usd=Decimal("10"),
        idempotency_key="idem_existing",
    )

    assert workload_id == "wkl_existing"
    assert gate._memory_spend == {}
    budget_gate.try_launch_admission.assert_awaited_once()


@pytest.mark.asyncio
async def test_sub_budget_gate_idempotency_replay_bypasses_sub_budget_rejection() -> None:
    budget_gate = _mock_budget_gate(admitted_id="wkl_existing")
    budget_gate.try_launch_admission = AsyncMock(
        return_value=BudgetAdmission(workload_id="wkl_existing", is_new=False)
    )
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("100"))],
    )
    gate = SubBudgetGate(
        budget_gate,
        config,
        tag_mtd_spend=lambda _tag: AsyncMock(return_value=Decimal("95"))(),
    )

    workload_id = await gate.try_launch(
        tag="ml",
        capability_id="cap_test",
        provider_id="prov_test",
        estimate_usd=Decimal("10"),
        idempotency_key="idem_existing",
    )

    assert workload_id == "wkl_existing"
    assert gate._memory_spend == {}
    budget_gate.try_launch_admission.assert_awaited_once()


@pytest.mark.asyncio
async def test_sub_budget_gate_idempotency_replay_does_not_increment_memory_spend() -> None:
    budget_gate = _mock_budget_gate(admitted_id="wkl_existing")
    budget_gate.try_launch_admission = AsyncMock(
        return_value=BudgetAdmission(workload_id="wkl_existing", is_new=False)
    )
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("100"))],
    )
    gate = SubBudgetGate(budget_gate, config)
    gate._memory_spend["ml"] = Decimal("30")

    workload_id = await gate.try_launch(
        tag="ml",
        capability_id="cap_test",
        provider_id="prov_test",
        estimate_usd=Decimal("10"),
        idempotency_key="idem_existing",
    )

    assert workload_id == "wkl_existing"
    assert gate._memory_spend["ml"] == Decimal("30")


@pytest.mark.asyncio
async def test_sub_budget_gate_delegates_mtd_spend() -> None:
    budget_gate = _mock_budget_gate()
    budget_gate.current_mtd_spend = AsyncMock(return_value=Decimal("123.456789"))
    config = SubBudgetConfig(total_budget_usd=Decimal("1000"))
    gate = SubBudgetGate(budget_gate, config)

    spend = await gate.current_mtd_spend()
    assert spend == Decimal("123.456789")


def test_sub_budget_gate_exposes_monthly_budget() -> None:
    budget_gate = _mock_budget_gate(monthly=Decimal("5000"))
    config = SubBudgetConfig(total_budget_usd=Decimal("5000"))
    gate = SubBudgetGate(budget_gate, config)
    assert gate.monthly_budget_usd == Decimal("5000")


# ---------------------------------------------------------------------------
# generate_chargeback_report
# ---------------------------------------------------------------------------


def test_chargeback_empty_workloads() -> None:
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("400"))],
    )
    report = generate_chargeback_report(config, [])
    assert report.total_spend_usd == Decimal("0")
    assert report.unallocated_spend_usd == Decimal("0")
    assert len(report.line_items) == 1
    assert report.line_items[0].spend_usd == Decimal("0")


def test_chargeback_prefers_actual_over_estimate() -> None:
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("400"))],
    )
    workloads = [
        {"cost_actual_usd": Decimal("10"), "cost_estimate_usd": Decimal("5")},
    ]
    report = generate_chargeback_report(config, workloads, tag_resolver=lambda _wl: "ml")
    assert report.line_items[0].spend_usd == Decimal("10")


def test_chargeback_fallback_to_estimate() -> None:
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("400"))],
    )
    workloads = [
        {"cost_estimate_usd": Decimal("25")},
    ]
    report = generate_chargeback_report(config, workloads, tag_resolver=lambda _wl: "ml")
    assert report.line_items[0].spend_usd == Decimal("25")


def test_chargeback_unallocated_when_no_resolver() -> None:
    config = SubBudgetConfig(total_budget_usd=Decimal("1000"))
    workloads = [{"cost_actual_usd": Decimal("99")}]
    report = generate_chargeback_report(config, workloads)
    assert report.unallocated_spend_usd == Decimal("99")
    assert report.total_spend_usd == Decimal("99")


def test_chargeback_unallocated_when_resolver_returns_none() -> None:
    config = SubBudgetConfig(total_budget_usd=Decimal("1000"))
    workloads = [{"cost_actual_usd": Decimal("99")}]
    report = generate_chargeback_report(config, workloads, tag_resolver=lambda _wl: None)
    assert report.unallocated_spend_usd == Decimal("99")


def test_chargeback_multiple_tags() -> None:
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[
            SubBudget(tag="ml", allocation_usd=Decimal("400")),
            SubBudget(tag="infra", allocation_usd=Decimal("300")),
        ],
    )
    workloads = [
        {"cost_actual_usd": Decimal("100"), "capability_id": "cap_ml"},
        {"cost_actual_usd": Decimal("50"), "capability_id": "cap_ml"},
        {"cost_actual_usd": Decimal("75"), "capability_id": "cap_infra"},
    ]
    report = generate_chargeback_report(
        config,
        workloads,
        tag_resolver=lambda wl: "ml" if wl["capability_id"] == "cap_ml" else "infra",
    )

    assert report.total_spend_usd == Decimal("225")
    ml_item = next(li for li in report.line_items if li.tag == "ml")
    infra_item = next(li for li in report.line_items if li.tag == "infra")
    assert ml_item.spend_usd == Decimal("150")
    assert ml_item.remaining_usd == Decimal("250")
    assert infra_item.spend_usd == Decimal("75")
    assert infra_item.remaining_usd == Decimal("225")


def test_chargeback_remaining_clamped_at_zero() -> None:
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("100"))],
    )
    workloads = [{"cost_actual_usd": Decimal("200")}]
    report = generate_chargeback_report(config, workloads, tag_resolver=lambda _wl: "ml")
    assert report.line_items[0].remaining_usd == Decimal("0")


def test_chargeback_skips_workloads_without_cost() -> None:
    config = SubBudgetConfig(total_budget_usd=Decimal("1000"))
    workloads: list[dict[str, Any]] = [
        {"state": "running"},
        {"cost_estimate_usd": Decimal("10")},
    ]
    report = generate_chargeback_report(config, workloads)
    assert report.total_spend_usd == Decimal("10")


def test_chargeback_serializable_dict() -> None:
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("400"))],
    )
    report = generate_chargeback_report(
        config,
        [{"cost_actual_usd": Decimal("123.456789")}],
        tag_resolver=lambda _wl: "ml",
    )
    d = report.to_serializable_dict()
    assert d["total_spend_usd"] == "123.456789"
    assert d["unallocated_spend_usd"] == "0.000000"
    assert d["line_items"][0]["spend_usd"] == "123.456789"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_chargeback_ignores_invalid_cost_values() -> None:
    config = SubBudgetConfig(total_budget_usd=Decimal("1000"))
    workloads: list[dict[str, Any]] = [
        {"cost_actual_usd": "not_a_number"},
        {"cost_estimate_usd": Decimal("10")},
    ]
    report = generate_chargeback_report(config, workloads)
    assert report.total_spend_usd == Decimal("10")


def test_sub_budget_config_from_strings() -> None:
    cfg = SubBudgetConfig(
        total_budget_usd=Decimal("1000.00"),
        budgets=[SubBudget(tag="a", allocation_usd=Decimal("500.00"))],
    )
    assert cfg.total_budget_usd == Decimal("1000.00")
    assert cfg.allocation_for("a") == Decimal("500.00")


@pytest.mark.asyncio
async def test_sub_budget_gate_with_async_tag_resolver() -> None:
    async def _resolver(tag: str) -> Decimal:
        return Decimal("10")

    budget_gate = _mock_budget_gate()
    config = SubBudgetConfig(
        total_budget_usd=Decimal("1000"),
        budgets=[SubBudget(tag="ml", allocation_usd=Decimal("100"))],
    )
    gate = SubBudgetGate(budget_gate, config, tag_mtd_spend=_resolver)

    workload_id = await gate.try_launch(
        tag="ml",
        capability_id="cap_test",
        provider_id="prov_test",
        estimate_usd=Decimal("50"),
    )
    assert workload_id == "wkl_001"

    with pytest.raises(SubBudgetRejected):
        await gate.try_launch(
            tag="ml",
            capability_id="cap_test",
            provider_id="prov_test",
            estimate_usd=Decimal("95"),
        )
