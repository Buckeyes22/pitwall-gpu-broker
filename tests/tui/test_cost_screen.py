"""Hermetic tests for the Textual Cost screen."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from textual.widgets import Static

from pitwall.cost.simulator import WhatIfBatchProjection
from pitwall.cost.sub_budgets import ChargebackLineItem, ChargebackReport
from pitwall.finops.burn_rate import BurnRateForecast
from pitwall.tui import PitwallApp
from pitwall.tui.cost import (
    CostSnapshot,
    StaticCostSource,
    format_cost_table,
    format_days,
    format_percent,
)
from pitwall.tui.overview import OverviewSnapshot, StaticOverviewSource
from pitwall.tui.providers import ProvidersSnapshot, StaticProvidersSource

pytestmark = pytest.mark.anyio

_NOW = dt.datetime(2026, 6, 2, 16, 30, tzinfo=dt.UTC)


def _overview_snapshot() -> OverviewSnapshot:
    return OverviewSnapshot(
        provider_total=3,
        provider_enabled=3,
        provider_health_counts={"healthy": 3},
        lease_state_counts={},
        active_leases=0,
        total_cost_usd=Decimal("0"),
        cost_entry_count=0,
        recent_workload_count=0,
        refreshed_at=_NOW,
    )


def _cost_snapshot() -> CostSnapshot:
    return CostSnapshot(
        runway=BurnRateForecast(
            burn_rate_usd_per_day=Decimal("12.500000"),
            projected_exhaustion=dt.datetime(2026, 6, 10, 16, 30, tzinfo=dt.UTC),
            trend="stable",
            confidence=Decimal("0.860000"),
            budget_usd=Decimal("100.000000"),
            remaining_budget_usd=Decimal("50.000000"),
            runway_days=Decimal("4.000000"),
        ),
        chargeback=ChargebackReport(
            total_spend_usd=Decimal("45.000000"),
            line_items=(
                ChargebackLineItem(
                    tag="ml",
                    allocation_usd=Decimal("60.000000"),
                    spend_usd=Decimal("30.000000"),
                    remaining_usd=Decimal("30.000000"),
                ),
                ChargebackLineItem(
                    tag="infra",
                    allocation_usd=Decimal("30.000000"),
                    spend_usd=Decimal("15.000000"),
                    remaining_usd=Decimal("15.000000"),
                ),
            ),
            unallocated_spend_usd=Decimal("5.000000"),
        ),
        what_if=WhatIfBatchProjection(
            projections=(),
            total_reserved_usd=Decimal("1.250000"),
            starting_spend_usd=Decimal("45.000000"),
            projected_spend_usd=Decimal("46.250000"),
            budget_usd=Decimal("100.000000"),
            budget_headroom_usd=Decimal("53.750000"),
            would_exceed_budget=False,
        ),
        refreshed_at=_NOW,
    )


def test_cost_snapshot_summaries_are_stable() -> None:
    snapshot = _cost_snapshot()

    assert snapshot.runway_summary == (
        "Burn: $12.50/day | Remaining: $50.00 | Runway: 4.0 days | "
        "Trend: stable | Confidence: 86.0%"
    )
    assert snapshot.sub_budget_summary == (
        "Sub-budgets: $45.00 spend across 2 tags | $5.00 unallocated"
    )
    assert snapshot.what_if_summary == (
        "What-if: reserves $1.25 | projected spend $46.25 | headroom $53.75 | within budget"
    )
    assert snapshot.refreshed_label == "2026-06-02 16:30 UTC"


@pytest.mark.parametrize(
    ("runway_days", "expected"),
    [
        (None, "unavailable"),
        (Decimal("1"), "1.0 day"),
        (Decimal("2.50"), "2.5 days"),
    ],
)
def test_format_days_handles_unknown_and_pluralization(
    runway_days: Decimal | None,
    expected: str,
) -> None:
    assert format_days(runway_days) == expected


@pytest.mark.property
@given(
    spend_cents=st.integers(min_value=0, max_value=10_000_000),
    allocation_cents=st.integers(min_value=1, max_value=10_000_000),
)
def test_format_percent_is_bounded_for_non_negative_spend(
    spend_cents: int,
    allocation_cents: int,
) -> None:
    percent = format_percent(
        Decimal(spend_cents) / Decimal(100),
        Decimal(allocation_cents) / Decimal(100),
    )

    numeric = Decimal(percent.removesuffix("%"))
    assert Decimal("0.0") <= numeric <= Decimal("100.0")


def test_format_cost_table_renders_sub_budget_rows() -> None:
    table = format_cost_table(_cost_snapshot().chargeback.line_items)

    assert "Tag" in table
    assert "Allocation" in table
    assert "Spend" in table
    assert "Remaining" in table
    assert "Used" in table
    assert "ml" in table
    assert "$60.00" in table
    assert "$30.00" in table
    assert "50.0%" in table


async def test_static_cost_source_loads_snapshot_and_counts_refreshes() -> None:
    source = StaticCostSource(_cost_snapshot())

    first = await source.load_cost()
    second = await source.load_cost()

    assert first == second
    assert source.load_count == 2


async def test_pitwall_app_switches_to_cost_screen() -> None:
    app = PitwallApp(
        overview_source=StaticOverviewSource(_overview_snapshot()),
        providers_source=StaticProvidersSource(ProvidersSnapshot(entries=())),
        cost_source=StaticCostSource(_cost_snapshot()),
    )

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()

        assert app.screen.name == "cost"
        assert str(app.screen.query_one("#cost-title", Static).content) == "Cost"


async def test_cost_screen_renders_snapshot() -> None:
    source = StaticCostSource(_cost_snapshot())
    app = PitwallApp(
        overview_source=StaticOverviewSource(_overview_snapshot()),
        providers_source=StaticProvidersSource(ProvidersSnapshot(entries=())),
        cost_source=source,
    )

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.press("c")
        await pilot.pause()

        assert source.load_count == 1
        assert "Burn: $12.50/day" in str(app.screen.query_one("#runway-summary", Static).content)
        assert "Sub-budgets: $45.00 spend across 2 tags" in str(
            app.screen.query_one("#sub-budget-summary", Static).content
        )
        assert "What-if: reserves $1.25" in str(
            app.screen.query_one("#what-if-summary", Static).content
        )
        assert "ml" in str(app.screen.query_one("#sub-budget-table", Static).content)
        assert "Last refreshed: 2026-06-02 16:30 UTC" in str(
            app.screen.query_one("#cost-refreshed", Static).content
        )


async def test_cost_screen_refresh_reloads_source() -> None:
    source = StaticCostSource(_cost_snapshot())
    app = PitwallApp(
        overview_source=StaticOverviewSource(_overview_snapshot()),
        providers_source=StaticProvidersSource(ProvidersSnapshot(entries=())),
        cost_source=source,
    )

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()

        assert source.load_count == 2


async def test_cost_screen_reports_source_failure() -> None:
    class FailingCostSource:
        async def load_cost(self) -> CostSnapshot:
            raise RuntimeError("boom")

    app = PitwallApp(
        overview_source=StaticOverviewSource(_overview_snapshot()),
        providers_source=StaticProvidersSource(ProvidersSnapshot(entries=())),
        cost_source=FailingCostSource(),
    )

    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.press("c")
        await pilot.pause()

        assert str(app.screen.query_one("#cost-error", Static).content) == "Cost unavailable: boom"
