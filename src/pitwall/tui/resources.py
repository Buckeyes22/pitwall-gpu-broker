"""RunPod resources screen and read-only state sources for the Textual console."""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from pitwall.runpod_client.mounts import NetworkVolume, NetworkVolumeClient
from pitwall.runpod_client.registry import (
    ContainerRegistryAuth,
    list_container_registry_auths,
)
from pitwall.runpod_client.serverless import Endpoint, list_endpoints
from pitwall.runpod_client.templates import HubTemplate, list_hub_templates
from pitwall.tui.errors import source_failure_message
from pitwall.tui.overview import as_utc, utc_now

_ENDPOINT_TABLE_HEADER = ("Endpoint ID", "Name", "Workers", "Template")
_TEMPLATE_TABLE_HEADER = ("Template ID", "Name", "Image", "Serverless")
_VOLUME_TABLE_HEADER = ("Volume ID", "Name", "Size", "Datacenter")
_REGISTRY_TABLE_HEADER = ("Auth ID", "Name")

EndpointLoader = Callable[[], Awaitable[Sequence[Endpoint]]]
TemplateLoader = Callable[[], Awaitable[Sequence[HubTemplate]]]
RegistryAuthLoader = Callable[[], Awaitable[Sequence[ContainerRegistryAuth]]]
VolumeClientFactory = Callable[[], "VolumeClient"]


class VolumeClient(Protocol):
    """Read-only volume client used by the Resources source."""

    async def list(self) -> Sequence[NetworkVolume]:
        """Return network volumes visible to the RunPod account."""

    async def aclose(self) -> None:
        """Close client resources."""


class ResourcesSource(Protocol):
    """Async provider for a single Resources refresh."""

    async def load_resources(self) -> ResourcesSnapshot:
        """Return the current RunPod resources snapshot."""


@dataclass(frozen=True, slots=True)
class ResourceEndpointEntry:
    """Read-only Serverless endpoint row rendered by the Resources screen."""

    endpoint_id: str
    name: str
    workers_min: int
    workers_max: int
    template_id: str | None

    @property
    def workers_label(self) -> str:
        return f"{self.workers_min}-{self.workers_max}"

    @property
    def template_label(self) -> str:
        return display_text(self.template_id, fallback="none")

    def as_row(self) -> tuple[str, str, str, str]:
        """Return table cell values in display order."""

        return (
            display_text(self.endpoint_id),
            display_text(self.name),
            self.workers_label,
            self.template_label,
        )


@dataclass(frozen=True, slots=True)
class ResourceTemplateEntry:
    """Read-only RunPod template row rendered by the Resources screen."""

    template_id: str
    name: str
    image_name: str
    is_serverless: bool

    @property
    def serverless_label(self) -> str:
        return "yes" if self.is_serverless else "no"

    def as_row(self) -> tuple[str, str, str, str]:
        """Return table cell values in display order."""

        return (
            display_text(self.template_id),
            display_text(self.name),
            display_text(self.image_name),
            self.serverless_label,
        )


@dataclass(frozen=True, slots=True)
class ResourceVolumeEntry:
    """Read-only network volume row rendered by the Resources screen."""

    volume_id: str
    name: str
    size_gb: int
    data_center_id: str

    @property
    def size_label(self) -> str:
        return f"{self.size_gb} GB"

    def as_row(self) -> tuple[str, str, str, str]:
        """Return table cell values in display order."""

        return (
            display_text(self.volume_id),
            display_text(self.name),
            self.size_label,
            display_text(self.data_center_id),
        )


@dataclass(frozen=True, slots=True)
class RegistryAuthEntry:
    """Read-only container registry auth row rendered by the Resources screen."""

    auth_id: str
    name: str

    def as_row(self) -> tuple[str, str]:
        """Return table cell values in display order."""

        return (display_text(self.auth_id), display_text(self.name))


@dataclass(frozen=True, slots=True)
class ResourcesSnapshot:
    """Read-only state rendered by the Resources screen."""

    endpoints: tuple[ResourceEndpointEntry, ...]
    templates: tuple[ResourceTemplateEntry, ...]
    volumes: tuple[ResourceVolumeEntry, ...]
    registry_auths: tuple[RegistryAuthEntry, ...]
    refreshed_at: dt.datetime

    @property
    def summary(self) -> str:
        return (
            "Resources: "
            f"{_count_label(len(self.endpoints), 'endpoint')} | "
            f"{_count_label(len(self.templates), 'template')} | "
            f"{_count_label(len(self.volumes), 'volume')} | "
            f"{_count_label(len(self.registry_auths), 'registry auth')}"
        )

    @property
    def refreshed_label(self) -> str:
        refreshed = as_utc(self.refreshed_at)
        return refreshed.strftime("%Y-%m-%d %H:%M UTC")


class StaticResourcesSource:
    """Hermetic source used by tests and local demos."""

    def __init__(self, snapshot: ResourcesSnapshot) -> None:
        self._snapshot = snapshot
        self.load_count = 0

    async def load_resources(self) -> ResourcesSnapshot:
        self.load_count += 1
        return self._snapshot


class RunPodResourcesSource:
    """Read RunPod resource inventory through existing read-only client APIs."""

    def __init__(
        self,
        *,
        endpoint_loader: EndpointLoader | None = None,
        template_loader: TemplateLoader | None = None,
        volume_client_factory: VolumeClientFactory | None = None,
        registry_auth_loader: RegistryAuthLoader | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._endpoint_loader = endpoint_loader or _default_endpoint_loader
        self._template_loader = template_loader or _default_template_loader
        self._volume_client_factory = volume_client_factory or _default_volume_client_factory
        self._registry_auth_loader = registry_auth_loader or _default_registry_auth_loader
        self._now = now or utc_now

    async def load_resources(self) -> ResourcesSnapshot:
        volume_client = self._volume_client_factory()
        try:
            endpoints = await self._endpoint_loader()
            templates = await self._template_loader()
            volumes = await volume_client.list()
            registry_auths = await self._registry_auth_loader()
        finally:
            await volume_client.aclose()

        return ResourcesSnapshot(
            endpoints=tuple(_endpoint_entry(endpoint) for endpoint in endpoints),
            templates=tuple(_template_entry(template) for template in templates),
            volumes=tuple(_volume_entry(volume) for volume in volumes),
            registry_auths=tuple(_registry_auth_entry(auth) for auth in registry_auths),
            refreshed_at=as_utc(self._now()),
        )


class ResourcesScreen(Screen[None]):
    """Read-only RunPod Resources screen in the operator console."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, source: ResourcesSource) -> None:
        super().__init__(name="resources")
        self._source = source

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="shell"):
            with Vertical(id="nav-panel"):
                yield Label("Pitwall", id="nav-title")
                yield ListView(
                    ListItem(Label("Overview"), id="nav-overview"),
                    ListItem(Label("Providers"), id="nav-providers"),
                    ListItem(Label("Leases"), id="nav-leases"),
                    ListItem(Label("Cost"), id="nav-cost"),
                    ListItem(Label("Resources"), id="nav-resources"),
                    id="shell-nav",
                )
            with Vertical(id="resources-panel"):
                yield Static("Resources", id="resources-title")
                yield Static("Loading resources", id="resources-summary", classes="summary")
                yield Static("Endpoints", classes="resource-section")
                yield Static("Loading endpoints", id="endpoints-table", classes="resource-table")
                yield Static("Templates", classes="resource-section")
                yield Static("Loading templates", id="templates-table", classes="resource-table")
                yield Static("Volumes", classes="resource-section")
                yield Static("Loading volumes", id="volumes-table", classes="resource-table")
                yield Static("Registry auths", classes="resource-section")
                yield Static(
                    "Loading registry auths", id="registry-table", classes="resource-table"
                )
                yield Static("", id="resources-refreshed", classes="summary")
                yield Static("", id="resources-error", classes="error")
        yield Footer()

    async def on_mount(self) -> None:
        await self._refresh()

    async def action_refresh(self) -> None:
        await self._refresh()

    async def _refresh(self) -> None:
        self.query_one("#resources-error", Static).update("")
        try:
            snapshot = await self._source.load_resources()
        except (
            Exception
        ) as exc:  # reason: TUI refresh must degrade to an inline error, never crash the app
            self.query_one("#resources-error", Static).update(
                source_failure_message("Resources unavailable", exc)
            )
            return
        self._render_snapshot(snapshot)

    def _render_snapshot(self, snapshot: ResourcesSnapshot) -> None:
        self.query_one("#resources-summary", Static).update(snapshot.summary)
        self.query_one("#endpoints-table", Static).update(format_endpoint_table(snapshot.endpoints))
        self.query_one("#templates-table", Static).update(format_template_table(snapshot.templates))
        self.query_one("#volumes-table", Static).update(format_volume_table(snapshot.volumes))
        self.query_one("#registry-table", Static).update(
            format_registry_table(snapshot.registry_auths)
        )
        self.query_one("#resources-refreshed", Static).update(
            f"Last refreshed: {snapshot.refreshed_label}"
        )


def display_text(value: object, *, fallback: str = "unknown") -> str:
    """Return a stripped non-empty label for operator display."""

    if value is None:
        return fallback
    label = str(value).strip()
    return label or fallback


def format_endpoint_table(entries: tuple[ResourceEndpointEntry, ...]) -> str:
    """Render a stable fixed-width endpoint table."""

    rows: list[tuple[str, str, str, str]] = [_ENDPOINT_TABLE_HEADER]
    rows.extend(entry.as_row() for entry in entries)
    return _format_table(rows)


def format_template_table(entries: tuple[ResourceTemplateEntry, ...]) -> str:
    """Render a stable fixed-width template table."""

    rows: list[tuple[str, str, str, str]] = [_TEMPLATE_TABLE_HEADER]
    rows.extend(entry.as_row() for entry in entries)
    return _format_table(rows)


def format_volume_table(entries: tuple[ResourceVolumeEntry, ...]) -> str:
    """Render a stable fixed-width network volume table."""

    rows: list[tuple[str, str, str, str]] = [_VOLUME_TABLE_HEADER]
    rows.extend(entry.as_row() for entry in entries)
    return _format_table(rows)


def format_registry_table(entries: tuple[RegistryAuthEntry, ...]) -> str:
    """Render a stable fixed-width registry auth table."""

    rows: list[tuple[str, str]] = [_REGISTRY_TABLE_HEADER]
    rows.extend(entry.as_row() for entry in entries)
    return _format_table(rows)


async def _default_endpoint_loader() -> Sequence[Endpoint]:
    return await list_endpoints()


async def _default_template_loader() -> Sequence[HubTemplate]:
    return await list_hub_templates()


async def _default_registry_auth_loader() -> Sequence[ContainerRegistryAuth]:
    return await list_container_registry_auths()


def _default_volume_client_factory() -> VolumeClient:
    return NetworkVolumeClient()


def _endpoint_entry(endpoint: Endpoint) -> ResourceEndpointEntry:
    return ResourceEndpointEntry(
        endpoint_id=display_text(endpoint.id),
        name=display_text(endpoint.name),
        workers_min=endpoint.scaling.workers_min,
        workers_max=endpoint.scaling.workers_max,
        template_id=endpoint.template_id,
    )


def _template_entry(template: HubTemplate) -> ResourceTemplateEntry:
    return ResourceTemplateEntry(
        template_id=display_text(template.id),
        name=display_text(template.display_name or template.name),
        image_name=display_text(template.image_name),
        is_serverless=template.is_serverless,
    )


def _volume_entry(volume: NetworkVolume) -> ResourceVolumeEntry:
    return ResourceVolumeEntry(
        volume_id=display_text(volume.id),
        name=display_text(volume.name),
        size_gb=volume.size,
        data_center_id=display_text(volume.data_center_id),
    )


def _registry_auth_entry(auth: ContainerRegistryAuth) -> RegistryAuthEntry:
    return RegistryAuthEntry(
        auth_id=display_text(auth.id),
        name=display_text(auth.name),
    )


def _count_label(count: int, singular: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {singular}{suffix}"


def _format_table[Row: tuple[str, ...]](rows: list[Row]) -> str:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]

    rendered: list[str] = []
    for index, row in enumerate(rows):
        rendered.append(_format_row(row, widths))
        if index == 0:
            rendered.append("  ".join("-" * width for width in widths))
    return "\n".join(rendered)


def _format_row(row: tuple[str, ...], widths: list[int]) -> str:
    return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))


__all__ = [
    "RegistryAuthEntry",
    "ResourceEndpointEntry",
    "ResourceTemplateEntry",
    "ResourceVolumeEntry",
    "ResourcesScreen",
    "ResourcesSnapshot",
    "ResourcesSource",
    "RunPodResourcesSource",
    "StaticResourcesSource",
    "display_text",
    "format_endpoint_table",
    "format_registry_table",
    "format_template_table",
    "format_volume_table",
]
