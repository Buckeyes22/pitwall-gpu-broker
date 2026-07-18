"""Textual application shell for the Pitwall operator console."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import asyncpg
from textual.app import App
from textual.binding import Binding

from pitwall.tui.cost import CostScreen, CostSource, PostgresCostSource
from pitwall.tui.leases import LeasesScreen, LeasesSource, PostgresLeasesSource
from pitwall.tui.operations import OperationsScreen, OperationsSource, PostgresOperationsSource
from pitwall.tui.overview import OverviewScreen, OverviewSource, PostgresOverviewSource
from pitwall.tui.providers import ProvidersScreen, ProvidersSource, RegistryProvidersSource
from pitwall.tui.resources import ResourcesScreen, ResourcesSource, RunPodResourcesSource

PoolFactory = Callable[[], Awaitable[asyncpg.Pool]]


class PitwallApp(App[None]):
    """Read-only Textual shell for Pitwall operators."""

    TITLE = "Pitwall"
    SUB_TITLE = "Operator Console"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("o", "show_overview", "Overview"),
        Binding("p", "show_providers", "Providers"),
        Binding("l", "show_leases", "Leases"),
        Binding("c", "show_cost", "Cost"),
        Binding("e", "show_resources", "Resources"),
        Binding("a", "show_operations", "Operations"),
    ]
    CSS = """
    Screen {
        layout: vertical;
    }

    #shell {
        height: 1fr;
    }

    #nav-panel {
        width: 24;
        padding: 1;
        border-right: solid $primary;
        background: $panel;
    }

    #nav-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #shell-nav {
        height: auto;
    }

    #overview-panel {
        padding: 1 2;
        width: 1fr;
    }

    #leases-panel {
        padding: 1 2;
        width: 1fr;
    }

    #overview-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #providers-panel {
        padding: 1 2;
        width: 1fr;
    }

    #providers-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #leases-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #providers-summary {
        margin-bottom: 1;
    }

    #leases-table {
        height: 1fr;
        margin-top: 1;
    }

    #cost-panel {
        padding: 1 2;
        width: 1fr;
    }

    #cost-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #resources-panel {
        padding: 1 2;
        width: 1fr;
    }

    #resources-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #operations-panel {
        padding: 1 2;
        width: 1fr;
    }

    #operations-title {
        text-style: bold;
        margin-bottom: 1;
    }

    .resource-section {
        text-style: bold;
        margin-top: 1;
    }

    .operation-section {
        text-style: bold;
        margin-top: 1;
    }

    .resource-table {
        margin-bottom: 1;
    }

    .operation-table {
        margin-bottom: 1;
    }

    #sub-budget-table {
        margin-top: 1;
        margin-bottom: 1;
    }

    #metric-row {
        height: 5;
        margin-bottom: 1;
    }

    .metric {
        width: 1fr;
        height: 4;
        padding: 1 2;
        margin-right: 1;
        border: solid $accent;
    }

    .summary {
        height: 1;
        margin-top: 1;
    }

    .error {
        color: $error;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        overview_source: OverviewSource | None = None,
        providers_source: ProvidersSource | None = None,
        leases_source: LeasesSource | None = None,
        cost_source: CostSource | None = None,
        resources_source: ResourcesSource | None = None,
        operations_source: OperationsSource | None = None,
        pool_factory: PoolFactory | None = None,
    ) -> None:
        super().__init__()
        self._overview_source = overview_source
        self._providers_source = providers_source
        self._leases_source = leases_source
        self._pool_factory = pool_factory
        self._pool: asyncpg.Pool | None = None
        self._leases_installed = False
        self._cost_source = cost_source
        self._resources_source = resources_source
        self._operations_source = operations_source

    async def on_mount(self) -> None:
        await self._install_overview()
        self._install_providers()
        self._install_cost()
        self._install_resources()
        self._install_operations()
        await self.push_screen("overview")

    async def action_show_overview(self) -> None:
        if self.screen.name == "overview":
            return
        await self.switch_screen("overview")

    async def action_show_providers(self) -> None:
        if self.screen.name == "providers":
            return
        await self.switch_screen("providers")

    async def action_show_leases(self) -> None:
        if self.screen.name == "leases":
            return
        await self._install_leases()
        await self.switch_screen("leases")

    async def action_show_cost(self) -> None:
        if self.screen.name == "cost":
            return
        await self.switch_screen("cost")

    async def action_show_resources(self) -> None:
        if self.screen.name == "resources":
            return
        await self.switch_screen("resources")

    async def action_show_operations(self) -> None:
        if self.screen.name == "operations":
            return
        await self.switch_screen("operations")

    async def _install_overview(self) -> None:
        source = await self._resolve_overview_source()
        self.install_screen(OverviewScreen(source), "overview")

    def _install_providers(self) -> None:
        source = self._providers_source or RegistryProvidersSource()
        self._providers_source = source
        self.install_screen(ProvidersScreen(source), "providers")

    async def _install_leases(self) -> None:
        if self._leases_installed:
            return
        source = await self._resolve_leases_source()
        self.install_screen(LeasesScreen(source), "leases")
        self._leases_installed = True

    def _install_cost(self) -> None:
        source = self._cost_source or PostgresCostSource(pool_factory=self._resolve_pool)
        self._cost_source = source
        self.install_screen(CostScreen(source), "cost")

    def _install_resources(self) -> None:
        source = self._resources_source or RunPodResourcesSource()
        self._resources_source = source
        self.install_screen(ResourcesScreen(source), "resources")

    def _install_operations(self) -> None:
        source = self._operations_source or PostgresOperationsSource(
            pool_factory=self._resolve_pool
        )
        self._operations_source = source
        self.install_screen(OperationsScreen(source), "operations")

    async def _resolve_overview_source(self) -> OverviewSource:
        if self._overview_source is not None:
            return self._overview_source
        pool = await self._resolve_pool()
        self._overview_source = PostgresOverviewSource(pool)
        return self._overview_source

    async def _resolve_leases_source(self) -> LeasesSource:
        if self._leases_source is not None:
            return self._leases_source
        pool = await self._resolve_pool()
        self._leases_source = PostgresLeasesSource(pool)
        return self._leases_source

    async def _resolve_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            pool_factory = self._pool_factory or default_pool
            self._pool = await pool_factory()
        return self._pool


async def default_pool() -> asyncpg.Pool:
    """Return the default Pitwall asyncpg pool."""

    from pitwall.db import get_pool

    return await get_pool()


__all__ = [
    "PitwallApp",
]
