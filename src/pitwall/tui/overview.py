"""Overview screen and state source for the Textual console."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

import asyncpg
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from pitwall.core.cost_reporting import cost_summary, recent_workloads
from pitwall.db.repository import ProviderRepository
from pitwall.tui.errors import source_failure_message

_CENT = Decimal("0.01")


class OverviewSource(Protocol):
    """Async provider for a single Overview refresh."""

    async def load_overview(self) -> OverviewSnapshot:
        """Return the current broker overview snapshot."""


@dataclass(frozen=True)
class OverviewSnapshot:
    """Read-only state rendered by the Overview screen."""

    provider_total: int
    provider_enabled: int
    provider_health_counts: Mapping[str, int]
    lease_state_counts: Mapping[str, int]
    active_leases: int
    total_cost_usd: Decimal
    cost_entry_count: int
    recent_workload_count: int
    refreshed_at: dt.datetime

    @property
    def provider_summary(self) -> str:
        return f"{self.provider_total} providers, {self.provider_enabled} enabled"

    @property
    def provider_health_summary(self) -> str:
        return format_count_summary(self.provider_health_counts)

    @property
    def lease_state_summary(self) -> str:
        return format_count_summary(self.lease_state_counts)

    @property
    def cost_summary(self) -> str:
        return f"{format_usd(self.total_cost_usd)} across {self.cost_entry_count} daily entries"

    @property
    def refreshed_label(self) -> str:
        refreshed = as_utc(self.refreshed_at)
        return refreshed.strftime("%Y-%m-%d %H:%M UTC")


class StaticOverviewSource:
    """Hermetic source used by tests and local demos."""

    def __init__(self, snapshot: OverviewSnapshot) -> None:
        self._snapshot = snapshot
        self.load_count = 0

    async def load_overview(self) -> OverviewSnapshot:
        self.load_count += 1
        return self._snapshot


class PostgresOverviewSource:
    """Read broker state from Pitwall's Postgres-backed service tables."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._pool = pool
        self._now = now or utc_now

    async def load_overview(self) -> OverviewSnapshot:
        providers = await ProviderRepository(self._pool).list(limit=10_000)
        lease_state_counts = await _fetch_lease_state_counts(self._pool)
        cost_payload = await cost_summary(self._pool)
        workload_payload = await recent_workloads(self._pool, limit=20)

        total_cost = decimal_from(cost_payload.get("total_usd", 0))
        cost_entries = sequence_len(cost_payload.get("entries", []))
        recent_count = sequence_len(workload_payload.get("workloads", []))

        return OverviewSnapshot(
            provider_total=len(providers),
            provider_enabled=sum(1 for provider in providers if provider.enabled),
            provider_health_counts=count_statuses(provider.health_status for provider in providers),
            lease_state_counts=lease_state_counts,
            active_leases=lease_state_counts.get("active", 0),
            total_cost_usd=total_cost,
            cost_entry_count=cost_entries,
            recent_workload_count=recent_count,
            refreshed_at=as_utc(self._now()),
        )


class OverviewScreen(Screen[None]):
    """Read-only Overview screen in the operator console."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("o", "show_overview", "Overview", show=False),
    ]

    def __init__(self, source: OverviewSource) -> None:
        super().__init__(name="overview")
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
                    id="shell-nav",
                )
            with Vertical(id="overview-panel"):
                yield Static("Pitwall Overview", id="overview-title")
                with Horizontal(id="metric-row"):
                    yield Static("Loading providers", id="provider-count", classes="metric")
                    yield Static("Loading leases", id="lease-count", classes="metric")
                    yield Static("Loading cost", id="cost-total", classes="metric")
                    yield Static("Loading workloads", id="workload-count", classes="metric")
                yield Static("", id="provider-health", classes="summary")
                yield Static("", id="lease-states", classes="summary")
                yield Static("", id="cost-detail", classes="summary")
                yield Static("", id="last-refreshed", classes="summary")
                yield Static("", id="overview-error", classes="error")
        yield Footer()

    async def on_mount(self) -> None:
        await self._refresh()

    async def action_refresh(self) -> None:
        await self._refresh()

    async def action_show_overview(self) -> None:
        await self._refresh()

    async def _refresh(self) -> None:
        self.query_one("#overview-error", Static).update("")
        try:
            snapshot = await self._source.load_overview()
        except (
            Exception
        ) as exc:  # reason: TUI refresh must degrade to an inline error, never crash the app
            self.query_one("#overview-error", Static).update(
                source_failure_message("Overview unavailable", exc)
            )
            return
        self._render_snapshot(snapshot)

    def _render_snapshot(self, snapshot: OverviewSnapshot) -> None:
        self.query_one("#provider-count", Static).update(snapshot.provider_summary)
        self.query_one("#lease-count", Static).update(f"{snapshot.active_leases} active leases")
        self.query_one("#cost-total", Static).update(format_usd(snapshot.total_cost_usd))
        self.query_one("#workload-count", Static).update(
            f"{snapshot.recent_workload_count} recent workloads"
        )
        self.query_one("#provider-health", Static).update(
            f"Provider health: {snapshot.provider_health_summary}"
        )
        self.query_one("#lease-states", Static).update(
            f"Lease states: {snapshot.lease_state_summary}"
        )
        self.query_one("#cost-detail", Static).update(snapshot.cost_summary)
        self.query_one("#last-refreshed", Static).update(
            f"Last refreshed: {snapshot.refreshed_label}"
        )


def count_statuses(statuses: Iterable[str | None]) -> dict[str, int]:
    """Normalize status strings and count occurrences."""

    counts: dict[str, int] = {}
    for status in statuses:
        key = normalized_status(status)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def format_count_summary(counts: Mapping[str, int]) -> str:
    """Render a stable count summary for status/state maps."""

    normalized = normalize_count_map(counts)
    if not normalized:
        return "none"
    return " | ".join(f"{key} {value}" for key, value in sorted(normalized.items()))


def normalize_count_map(counts: Mapping[str, int]) -> dict[str, int]:
    """Normalize status/state count maps and drop zero counts."""

    normalized: dict[str, int] = {}
    for key, value in counts.items():
        if value <= 0:
            continue
        normalized_key = normalized_status(key)
        normalized[normalized_key] = normalized.get(normalized_key, 0) + value
    return normalized


def normalized_status(value: str | None) -> str:
    """Return a display-safe status key."""

    if value is None:
        return "unknown"
    stripped = value.strip().lower()
    return stripped or "unknown"


def format_usd(amount: Decimal) -> str:
    """Format a USD Decimal with cent rounding."""

    rounded = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    return f"${rounded:,.2f}"


def decimal_from(value: Any) -> Decimal:
    """Convert numeric DB payloads to Decimal without inheriting float artifacts."""

    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float | str):
        return Decimal(str(value))
    return Decimal("0")


def sequence_len(value: object) -> int:
    """Return length for sequence-like API payloads."""

    if isinstance(value, list | tuple):
        return len(value)
    return 0


def utc_now() -> dt.datetime:
    """Return current UTC time."""

    return dt.datetime.now(dt.UTC)


def as_utc(value: dt.datetime) -> dt.datetime:
    """Normalize a datetime for operator display."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


async def _fetch_lease_state_counts(pool: asyncpg.Pool) -> dict[str, int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT state, COUNT(*) AS lease_count
            FROM pitwall.leases
            GROUP BY state
            """
        )
    raw_counts: dict[str, int] = {}
    for row in rows:
        raw_counts[str(row["state"])] = int(row["lease_count"])
    return normalize_count_map(raw_counts)


__all__ = [
    "OverviewScreen",
    "OverviewSnapshot",
    "OverviewSource",
    "PostgresOverviewSource",
    "StaticOverviewSource",
    "as_utc",
    "count_statuses",
    "decimal_from",
    "format_count_summary",
    "format_usd",
    "normalize_count_map",
    "normalized_status",
    "sequence_len",
    "utc_now",
]
