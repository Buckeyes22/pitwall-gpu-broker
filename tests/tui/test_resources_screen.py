"""Hermetic tests for the Textual resources screen."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from textual.widgets import Static

from pitwall.runpod_client.mounts import NetworkVolume
from pitwall.runpod_client.registry import ContainerRegistryAuth
from pitwall.runpod_client.serverless import Endpoint, EndpointScalingConfig
from pitwall.runpod_client.templates import HubTemplate
from pitwall.tui import PitwallApp
from pitwall.tui.overview import OverviewSnapshot, StaticOverviewSource
from pitwall.tui.providers import ProvidersSnapshot, StaticProvidersSource
from pitwall.tui.resources import (
    RegistryAuthEntry,
    ResourceEndpointEntry,
    ResourcesSnapshot,
    ResourceTemplateEntry,
    ResourceVolumeEntry,
    RunPodResourcesSource,
    StaticResourcesSource,
    display_text,
    format_endpoint_table,
    format_registry_table,
    format_template_table,
    format_volume_table,
)

pytestmark = pytest.mark.anyio

_NOW = dt.datetime(2026, 6, 2, 16, 30, tzinfo=dt.UTC)


class FakeVolumeClient:
    def __init__(self, volumes: Sequence[NetworkVolume]) -> None:
        self._volumes = volumes
        self.closed = False

    async def list(self) -> Sequence[NetworkVolume]:
        return self._volumes

    async def aclose(self) -> None:
        self.closed = True


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


def _resources_snapshot() -> ResourcesSnapshot:
    return ResourcesSnapshot(
        endpoints=(
            ResourceEndpointEntry(
                endpoint_id="ep_alpha",
                name="alpha-endpoint",
                workers_min=1,
                workers_max=3,
                template_id="tmpl_alpha",
            ),
            ResourceEndpointEntry(
                endpoint_id="ep_beta",
                name="beta-endpoint",
                workers_min=0,
                workers_max=1,
                template_id=None,
            ),
        ),
        templates=(
            ResourceTemplateEntry(
                template_id="tmpl_alpha",
                name="Alpha Template",
                image_name="ghcr.io/example/worker:abc123",
                is_serverless=True,
            ),
        ),
        volumes=(
            ResourceVolumeEntry(
                volume_id="vol_alpha",
                name="weights-alpha",
                size_gb=80,
                data_center_id="US-KS-1",
            ),
        ),
        registry_auths=(
            RegistryAuthEntry(
                auth_id="auth_alpha",
                name="ghcr-pitwall",
            ),
        ),
        refreshed_at=_NOW,
    )


@pytest.mark.property
@given(st.text(max_size=32))
def test_display_text_returns_display_safe_non_empty_label(value: str) -> None:
    label = display_text(value)

    assert label
    assert label == label.strip()


def test_resources_snapshot_summary_is_stable() -> None:
    snapshot = _resources_snapshot()

    assert snapshot.summary == ("Resources: 2 endpoints | 1 template | 1 volume | 1 registry auth")
    assert snapshot.refreshed_label == "2026-06-02 16:30 UTC"


def test_resource_tables_render_all_resource_sections() -> None:
    snapshot = _resources_snapshot()

    endpoint_table = format_endpoint_table(snapshot.endpoints)
    template_table = format_template_table(snapshot.templates)
    volume_table = format_volume_table(snapshot.volumes)
    registry_table = format_registry_table(snapshot.registry_auths)

    assert "Endpoint ID" in endpoint_table
    assert "ep_alpha" in endpoint_table
    assert "1-3" in endpoint_table
    assert "none" in endpoint_table
    assert "Template ID" in template_table
    assert "Alpha Template" in template_table
    assert "yes" in template_table
    assert "Volume ID" in volume_table
    assert "80 GB" in volume_table
    assert "US-KS-1" in volume_table
    assert "Auth ID" in registry_table
    assert "ghcr-pitwall" in registry_table


async def test_runpod_resources_source_maps_read_only_clients() -> None:
    async def endpoint_loader() -> Sequence[Endpoint]:
        return (
            Endpoint(
                id="ep_live",
                name="live-endpoint",
                scaling=EndpointScalingConfig(workers_min=2, workers_max=5),
                template_id="tmpl_live",
            ),
        )

    async def template_loader() -> Sequence[HubTemplate]:
        return (
            HubTemplate(
                id="tmpl_live",
                name="worker-live",
                display_name="Worker Live",
                image_name="ghcr.io/example/worker:live",
                is_serverless=True,
            ),
        )

    async def registry_auth_loader() -> Sequence[ContainerRegistryAuth]:
        return (ContainerRegistryAuth(id="auth_live", name="ghcr-live"),)

    volume_client = FakeVolumeClient(
        (NetworkVolume(id="vol_live", name="weights-live", size=120, data_center_id="EU-RO-1"),)
    )
    source = RunPodResourcesSource(
        endpoint_loader=endpoint_loader,
        template_loader=template_loader,
        volume_client_factory=lambda: volume_client,
        registry_auth_loader=registry_auth_loader,
        now=lambda: _NOW,
    )

    snapshot = await source.load_resources()

    assert snapshot.endpoints[0].as_row() == ("ep_live", "live-endpoint", "2-5", "tmpl_live")
    assert snapshot.templates[0].as_row() == (
        "tmpl_live",
        "Worker Live",
        "ghcr.io/example/worker:live",
        "yes",
    )
    assert snapshot.volumes[0].as_row() == ("vol_live", "weights-live", "120 GB", "EU-RO-1")
    assert snapshot.registry_auths[0].as_row() == ("auth_live", "ghcr-live")
    assert snapshot.refreshed_at == _NOW
    assert volume_client.closed


async def test_pitwall_app_switches_to_resources_screen() -> None:
    app = PitwallApp(
        overview_source=StaticOverviewSource(_overview_snapshot()),
        providers_source=StaticProvidersSource(ProvidersSnapshot(entries=())),
        resources_source=StaticResourcesSource(_resources_snapshot()),
    )

    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()

        assert app.screen.name == "resources"
        assert str(app.screen.query_one("#resources-title", Static).content) == "Resources"


async def test_resources_screen_renders_snapshot() -> None:
    source = StaticResourcesSource(_resources_snapshot())
    app = PitwallApp(
        overview_source=StaticOverviewSource(_overview_snapshot()),
        providers_source=StaticProvidersSource(ProvidersSnapshot(entries=())),
        resources_source=source,
    )

    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.press("e")
        await pilot.pause()

        assert source.load_count == 1
        assert "Resources: 2 endpoints" in str(
            app.screen.query_one("#resources-summary", Static).content
        )
        assert "ep_alpha" in str(app.screen.query_one("#endpoints-table", Static).content)
        assert "Alpha Template" in str(app.screen.query_one("#templates-table", Static).content)
        assert "weights-alpha" in str(app.screen.query_one("#volumes-table", Static).content)
        assert "ghcr-pitwall" in str(app.screen.query_one("#registry-table", Static).content)
        assert "Last refreshed: 2026-06-02 16:30 UTC" in str(
            app.screen.query_one("#resources-refreshed", Static).content
        )


async def test_resources_screen_refresh_reloads_source() -> None:
    source = StaticResourcesSource(_resources_snapshot())
    app = PitwallApp(
        overview_source=StaticOverviewSource(_overview_snapshot()),
        providers_source=StaticProvidersSource(ProvidersSnapshot(entries=())),
        resources_source=source,
    )

    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()

        assert source.load_count == 2


async def test_resources_screen_reports_source_failure() -> None:
    class FailingResourcesSource:
        async def load_resources(self) -> ResourcesSnapshot:
            raise RuntimeError("boom")

    app = PitwallApp(
        overview_source=StaticOverviewSource(_overview_snapshot()),
        providers_source=StaticProvidersSource(ProvidersSnapshot(entries=())),
        resources_source=FailingResourcesSource(),
    )

    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.press("e")
        await pilot.pause()

        assert str(app.screen.query_one("#resources-error", Static).content) == (
            "Resources unavailable: boom"
        )
