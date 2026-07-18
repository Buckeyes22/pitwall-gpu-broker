"""Providers screen and registry-backed state source for the Textual console."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from pitwall.providers.registry import ProviderRegistry, get_default_registry
from pitwall.tui.errors import source_failure_message

RegistryFactory = Callable[[], ProviderRegistry]

_PROVIDER_TABLE_HEADER = ("Provider ID", "Status", "Pricing model")


class ProvidersSource(Protocol):
    """Async provider for a single Providers refresh."""

    async def load_providers(self) -> ProvidersSnapshot:
        """Return the current registered provider plugin snapshot."""


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    """Read-only provider plugin row rendered by the Providers screen."""

    provider_id: str
    status: str
    pricing_model: str

    def as_row(self) -> tuple[str, str, str]:
        """Return table cell values in display order."""

        return (self.provider_id, self.status, self.pricing_model)


@dataclass(frozen=True, slots=True)
class ProvidersSnapshot:
    """Read-only state rendered by the Providers screen."""

    entries: tuple[ProviderEntry, ...]

    @property
    def summary(self) -> str:
        noun = "provider" if len(self.entries) == 1 else "providers"
        return f"{len(self.entries)} registered {noun}"


class StaticProvidersSource:
    """Hermetic source used by tests and local demos."""

    def __init__(self, snapshot: ProvidersSnapshot) -> None:
        self._snapshot = snapshot
        self.load_count = 0

    async def load_providers(self) -> ProvidersSnapshot:
        self.load_count += 1
        return self._snapshot


class RegistryProvidersSource:
    """Read provider plugin metadata from the process provider registry."""

    def __init__(
        self,
        *,
        registry_factory: RegistryFactory = get_default_registry,
    ) -> None:
        self._registry_factory = registry_factory

    async def load_providers(self) -> ProvidersSnapshot:
        registry = self._registry_factory()
        entries = tuple(_provider_entry(registry, provider_id) for provider_id in registry.ids)
        return ProvidersSnapshot(entries=entries)


class ProvidersScreen(Screen[None]):
    """Read-only Providers screen in the operator console."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, source: ProvidersSource) -> None:
        super().__init__(name="providers")
        self._source = source

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="shell"):
            with Vertical(id="nav-panel"):
                yield Label("Pitwall", id="nav-title")
                yield ListView(
                    ListItem(Label("Overview"), id="nav-overview"),
                    ListItem(Label("Providers"), id="nav-providers"),
                    id="shell-nav",
                )
            with Vertical(id="providers-panel"):
                yield Static("Providers", id="providers-title")
                yield Static("", id="providers-summary", classes="summary")
                yield Static("Loading providers", id="providers-table")
                yield Static("", id="providers-error", classes="error")
        yield Footer()

    async def on_mount(self) -> None:
        await self._refresh()

    async def action_refresh(self) -> None:
        await self._refresh()

    async def _refresh(self) -> None:
        self.query_one("#providers-error", Static).update("")
        try:
            snapshot = await self._source.load_providers()
        except (
            Exception
        ) as exc:  # reason: TUI refresh must degrade to an inline error, never crash the app
            self.query_one("#providers-error", Static).update(
                source_failure_message("Providers unavailable", exc)
            )
            return
        self._render_snapshot(snapshot)

    def _render_snapshot(self, snapshot: ProvidersSnapshot) -> None:
        self.query_one("#providers-summary", Static).update(snapshot.summary)
        self.query_one("#providers-table", Static).update(format_providers_table(snapshot.entries))


def format_providers_table(entries: tuple[ProviderEntry, ...]) -> str:
    """Render a stable fixed-width provider table."""

    rows: list[tuple[str, str, str]] = [_PROVIDER_TABLE_HEADER]
    rows.extend(entry.as_row() for entry in entries)
    widths = [max(len(row[column]) for row in rows) for column in range(3)]

    rendered: list[str] = []
    for index, row in enumerate(rows):
        rendered.append(_format_row(row, widths))
        if index == 0:
            rendered.append("  ".join("-" * width for width in widths))
    return "\n".join(rendered)


def _format_row(row: tuple[str, str, str], widths: list[int]) -> str:
    return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))


def _provider_entry(registry: ProviderRegistry, provider_id: str) -> ProviderEntry:
    registry.lookup(provider_id)
    return ProviderEntry(
        provider_id=provider_id,
        status="registered",
        pricing_model="tagged",
    )


__all__ = [
    "ProviderEntry",
    "ProvidersScreen",
    "ProvidersSnapshot",
    "ProvidersSource",
    "RegistryProvidersSource",
    "StaticProvidersSource",
    "format_providers_table",
]
