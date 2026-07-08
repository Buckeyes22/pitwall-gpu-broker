"""Hermetic tests for the Textual leases screen."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from textual.widgets import DataTable, Static

from pitwall.tui import PitwallApp
from pitwall.tui.leases import (
    LeaseDisplayRow,
    LeasesSnapshot,
    StaticLeasesSource,
    format_optional_usd,
    normalized_status,
    readiness_label,
)
from pitwall.tui.overview import OverviewSnapshot, StaticOverviewSource

pytestmark = pytest.mark.anyio

_NOW = dt.datetime(2026, 6, 2, 16, 30, tzinfo=dt.UTC)
_EXPIRY = dt.datetime(2026, 6, 2, 18, 45, tzinfo=dt.UTC)


def _row(
    *,
    lease_id: str = "lease_alpha",
    provider_id: str = "provider_l4",
    pod_id: str = "pod_alpha",
    state: str = "active",
    readiness: str = "ready",
    expires_at: dt.datetime = _EXPIRY,
    cost_accrued_usd: Decimal | None = Decimal("1.234"),
) -> LeaseDisplayRow:
    return LeaseDisplayRow(
        lease_id=lease_id,
        provider_id=provider_id,
        pod_id=pod_id,
        state=state,
        readiness=readiness,
        expires_at=expires_at,
        cost_accrued_usd=cost_accrued_usd,
    )


def _snapshot(*rows: LeaseDisplayRow) -> LeasesSnapshot:
    return LeasesSnapshot(rows=tuple(rows), refreshed_at=_NOW)


def _overview_source() -> StaticOverviewSource:
    return StaticOverviewSource(
        OverviewSnapshot(
            provider_total=0,
            provider_enabled=0,
            provider_health_counts={},
            lease_state_counts={},
            active_leases=0,
            total_cost_usd=Decimal("0"),
            cost_entry_count=0,
            recent_workload_count=0,
            refreshed_at=_NOW,
        )
    )


def test_format_optional_usd_rounds_costs_and_marks_missing() -> None:
    assert format_optional_usd(Decimal("1.235")) == "$1.24"
    assert format_optional_usd(None) == "pending"


@pytest.mark.property
@given(st.text(max_size=16))
def test_normalized_status_returns_display_safe_non_empty_key(value: str) -> None:
    status = normalized_status(value)

    assert status
    assert status == status.strip().lower()


def test_readiness_label_distinguishes_ready_partial_and_pending() -> None:
    assert (
        readiness_label(
            {
                "runtime_seen_at": "2026-06-02T16:00:00Z",
                "port_mappings_seen_at": "2026-06-02T16:01:00Z",
                "probe_passed_at": "2026-06-02T16:02:00Z",
            }
        )
        == "ready"
    )
    assert readiness_label({"runtime_seen_at": "2026-06-02T16:00:00Z"}) == "partial"
    assert readiness_label(None) == "pending"


async def test_static_leases_source_counts_refreshes() -> None:
    source = StaticLeasesSource(_snapshot(_row()))

    assert await source.load_leases() == _snapshot(_row())
    assert await source.load_leases() == _snapshot(_row())
    assert source.load_count == 2


async def test_leases_screen_renders_rows_with_status_and_timestamps() -> None:
    source = StaticLeasesSource(
        _snapshot(
            _row(),
            _row(
                lease_id="lease_beta",
                pod_id="pod_beta",
                state="waiting_runtime",
                readiness="pending",
                cost_accrued_usd=None,
            ),
        )
    )
    app = PitwallApp(
        overview_source=_overview_source(),
        leases_source=source,
    )

    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.press("l")
        await pilot.pause()

        table = app.screen.query_one("#leases-table", DataTable)
        assert app.screen.name == "leases"
        assert str(app.screen.query_one("#leases-title", Static).content) == "Pods / Leases"
        assert str(app.screen.query_one("#leases-summary", Static).content) == (
            "2 active pod leases"
        )
        assert table.row_count == 2
        assert table.get_row_at(0) == [
            "lease_alpha",
            "pod_alpha",
            "provider_l4",
            "active",
            "ready",
            "2026-06-02 18:45 UTC",
            "$1.23",
        ]
        assert table.get_row_at(1)[3:5] == ["waiting_runtime", "pending"]


async def test_leases_screen_renders_empty_state() -> None:
    source = StaticLeasesSource(_snapshot())
    app = PitwallApp(overview_source=_overview_source(), leases_source=source)

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.press("l")
        await pilot.pause()

        assert str(app.screen.query_one("#leases-summary", Static).content) == (
            "0 active pod leases"
        )
        assert str(app.screen.query_one("#leases-empty", Static).content) == (
            "No active pod leases"
        )
        assert app.screen.query_one("#leases-table", DataTable).row_count == 0


async def test_leases_refresh_reloads_source() -> None:
    source = StaticLeasesSource(_snapshot(_row()))
    app = PitwallApp(overview_source=_overview_source(), leases_source=source)

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.press("l")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()

        assert source.load_count == 2


async def test_leases_screen_shows_source_failure_without_stale_rows() -> None:
    class FailingSource:
        async def load_leases(self) -> LeasesSnapshot:
            raise RuntimeError("boom")

    app = PitwallApp(overview_source=_overview_source(), leases_source=FailingSource())

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.press("l")
        await pilot.pause()

        assert str(app.screen.query_one("#leases-error", Static).content) == (
            "Pods / leases unavailable: boom"
        )
        assert app.screen.query_one("#leases-table", DataTable).row_count == 0


async def test_app_navigates_between_overview_and_leases() -> None:
    source = StaticLeasesSource(_snapshot(_row()))
    app = PitwallApp(overview_source=_overview_source(), leases_source=source)

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.press("l")
        await pilot.pause()
        assert app.screen.name == "leases"

        await pilot.press("o")
        await pilot.pause()
        assert app.screen.name == "overview"
