"""Hermetic tests for the Textual Providers screen."""

from __future__ import annotations

import pytest
from textual.widgets import Static

from pitwall.providers.registry import create_default_registry
from pitwall.tui import PitwallApp
from pitwall.tui.overview import OverviewSnapshot, StaticOverviewSource
from pitwall.tui.providers import (
    ProviderEntry,
    ProvidersSnapshot,
    RegistryProvidersSource,
    StaticProvidersSource,
)

pytestmark = pytest.mark.anyio


def _overview_snapshot() -> OverviewSnapshot:
    import datetime as dt
    from decimal import Decimal

    return OverviewSnapshot(
        provider_total=3,
        provider_enabled=3,
        provider_health_counts={"healthy": 3},
        lease_state_counts={},
        active_leases=0,
        total_cost_usd=Decimal("0"),
        cost_entry_count=0,
        recent_workload_count=0,
        refreshed_at=dt.datetime(2026, 6, 2, 16, 30, tzinfo=dt.UTC),
    )


def _providers_snapshot() -> ProvidersSnapshot:
    return ProvidersSnapshot(
        entries=(
            ProviderEntry(
                provider_id="runpod",
                status="registered",
                pricing_model="tagged",
            ),
            ProviderEntry(
                provider_id="vast",
                status="registered",
                pricing_model="tagged",
            ),
            ProviderEntry(
                provider_id="together",
                status="registered",
                pricing_model="tagged",
            ),
        )
    )


async def test_registry_provider_source_lists_default_registry_entries() -> None:
    source = RegistryProvidersSource(registry_factory=create_default_registry)

    snapshot = await source.load_providers()

    # Order-agnostic + not count-locked: the default registry grows as providers
    # land (runpod/vast/together/lambda_cloud …). Assert the known providers are
    # all present rather than pinning an exact list that breaks on every new one.
    listed = {entry.provider_id for entry in snapshot.entries}
    assert {"runpod", "vast", "together", "lambda_cloud"} <= listed
    assert all(entry.status == "registered" for entry in snapshot.entries)
    assert all(entry.pricing_model == "tagged" for entry in snapshot.entries)


async def test_pitwall_app_switches_to_providers_screen() -> None:
    app = PitwallApp(
        overview_source=StaticOverviewSource(_overview_snapshot()),
        providers_source=StaticProvidersSource(_providers_snapshot()),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()

        assert app.screen.name == "providers"
        assert str(app.screen.query_one("#providers-title", Static).content) == "Providers"


async def test_providers_screen_renders_registered_provider_rows() -> None:
    source = StaticProvidersSource(_providers_snapshot())
    app = PitwallApp(
        overview_source=StaticOverviewSource(_overview_snapshot()),
        providers_source=source,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("p")
        await pilot.pause()

        table = str(app.screen.query_one("#providers-table", Static).content)

        assert source.load_count == 1
        assert "Provider ID" in table
        assert "Status" in table
        assert "Pricing model" in table
        assert "runpod" in table
        assert "vast" in table
        assert "together" in table
        assert "registered" in table
        assert "tagged" in table


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "0 registered providers"), (1, "1 registered provider"), (2, "2 registered providers")],
)
def test_providers_snapshot_summary_pluralizes(count: int, expected: str) -> None:
    snapshot = ProvidersSnapshot(
        entries=tuple(
            ProviderEntry(
                provider_id=f"provider-{index}",
                status="registered",
                pricing_model="tagged",
            )
            for index in range(count)
        )
    )

    assert snapshot.summary == expected
