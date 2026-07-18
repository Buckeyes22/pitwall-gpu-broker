"""Hermetic tests for the Textual overview shell."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from textual.widgets import Static

from pitwall.tui import PitwallApp
from pitwall.tui.confirmation import ConfirmTier, confirm_tier_for_action
from pitwall.tui.overview import (
    OverviewSnapshot,
    StaticOverviewSource,
    count_statuses,
    format_usd,
)

pytestmark = pytest.mark.anyio

_NOW = dt.datetime(2026, 6, 2, 16, 30, tzinfo=dt.UTC)


def _snapshot() -> OverviewSnapshot:
    return OverviewSnapshot(
        provider_total=4,
        provider_enabled=3,
        provider_health_counts={"healthy": 2, "unhealthy": 1, "unknown": 1},
        lease_state_counts={"active": 2, "terminated": 1},
        active_leases=2,
        total_cost_usd=Decimal("12.345"),
        cost_entry_count=5,
        recent_workload_count=7,
        refreshed_at=_NOW,
    )


def test_format_usd_rounds_to_cents() -> None:
    assert format_usd(Decimal("12.345")) == "$12.35"
    assert format_usd(Decimal("0")) == "$0.00"


@pytest.mark.property
@given(st.lists(st.text(max_size=12), max_size=50))
def test_count_statuses_normalizes_non_empty_keys_and_preserves_total(
    statuses: list[str],
) -> None:
    counts = count_statuses(statuses)

    assert sum(counts.values()) == len(statuses)
    assert all(key == key.strip().lower() for key in counts)
    assert all(key for key in counts)


def test_snapshot_summaries_are_stable_and_sorted() -> None:
    snapshot = _snapshot()

    assert snapshot.provider_summary == "4 providers, 3 enabled"
    assert snapshot.provider_health_summary == "healthy 2 | unhealthy 1 | unknown 1"
    assert snapshot.lease_state_summary == "active 2 | terminated 1"
    assert snapshot.cost_summary == "$12.35 across 5 daily entries"


def test_destructive_actions_require_confirm_tiers() -> None:
    assert confirm_tier_for_action("overview.refresh") is ConfirmTier.NONE
    assert confirm_tier_for_action("lease.terminate") is ConfirmTier.TYPE_TO_CONFIRM
    assert confirm_tier_for_action("provider.disable") is ConfirmTier.DOUBLE_CONFIRM


async def test_pitwall_app_mounts_overview_screen() -> None:
    source = StaticOverviewSource(_snapshot())
    app = PitwallApp(overview_source=source)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        assert app.screen.name == "overview"
        assert str(app.screen.query_one("#overview-title", Static).content) == "Pitwall Overview"
        assert (
            str(app.screen.query_one("#provider-count", Static).content) == "4 providers, 3 enabled"
        )
        assert str(app.screen.query_one("#lease-count", Static).content) == "2 active leases"
        assert str(app.screen.query_one("#cost-total", Static).content) == "$12.35"
        assert "healthy 2 | unhealthy 1 | unknown 1" in str(
            app.screen.query_one("#provider-health", Static).content
        )


async def test_overview_refresh_reloads_source() -> None:
    source = StaticOverviewSource(_snapshot())
    app = PitwallApp(overview_source=source)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()

        assert source.load_count == 2
