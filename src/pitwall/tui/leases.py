"""Pods / leases screen and state source for the Textual console."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol, cast

import asyncpg
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label, ListItem, ListView, Static

from pitwall.leases.state import TERMINAL_LEASE_STATES
from pitwall.tui.errors import source_failure_message

_CENT = Decimal("0.01")
_READY_KEYS = frozenset(
    {
        "runtime_seen_at",
        "port_mappings_seen_at",
        "probe_passed_at",
    }
)


class LeasesSource(Protocol):
    """Async provider for a single Pods / Leases refresh."""

    async def load_leases(self) -> LeasesSnapshot:
        """Return active pod lease rows for the operator screen."""


@dataclass(frozen=True)
class LeaseDisplayRow:
    """Read-only lease row rendered in the Pods / Leases table."""

    lease_id: str
    provider_id: str
    pod_id: str
    state: str
    readiness: str
    expires_at: dt.datetime
    cost_accrued_usd: Decimal | None = None

    @property
    def state_label(self) -> str:
        return normalized_status(self.state)

    @property
    def expires_label(self) -> str:
        return format_utc(self.expires_at)

    @property
    def cost_label(self) -> str:
        return format_optional_usd(self.cost_accrued_usd)


@dataclass(frozen=True)
class LeasesSnapshot:
    """Read-only state rendered by the Pods / Leases screen."""

    rows: tuple[LeaseDisplayRow, ...]
    refreshed_at: dt.datetime

    @property
    def active_count(self) -> int:
        return len(self.rows)

    @property
    def refreshed_label(self) -> str:
        return format_utc(self.refreshed_at)


class StaticLeasesSource:
    """Hermetic source used by tests and local demos."""

    def __init__(self, snapshot: LeasesSnapshot) -> None:
        self._snapshot = snapshot
        self.load_count = 0

    async def load_leases(self) -> LeasesSnapshot:
        self.load_count += 1
        return self._snapshot


class PostgresLeasesSource:
    """Read active pod lease rows from Pitwall's Postgres-backed lease table."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        now: Callable[[], dt.datetime] | None = None,
        limit: int = 200,
    ) -> None:
        self._pool = pool
        self._now = now or utc_now
        self._limit = limit

    async def load_leases(self) -> LeasesSnapshot:
        terminal_states = tuple(state.value for state in TERMINAL_LEASE_STATES)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, provider_id, runpod_pod_id, state, expires_at,
                       readiness, cost_accrued_usd
                FROM pitwall.leases
                WHERE state <> ALL($1::text[])
                ORDER BY expires_at ASC, created_at ASC
                LIMIT $2
                """,
                terminal_states,
                self._limit,
            )

        return LeasesSnapshot(
            rows=tuple(_display_row_from_record(row) for row in rows),
            refreshed_at=as_utc(self._now()),
        )


class LeasesScreen(Screen[None]):
    """Read-only Pods / Leases screen in the operator console."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("o", "show_overview", "Overview"),
        Binding("l", "show_leases", "Leases", show=False),
    ]

    def __init__(self, source: LeasesSource) -> None:
        super().__init__(name="leases")
        self._source = source

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="shell"):
            with Vertical(id="nav-panel"):
                yield Label("Pitwall", id="nav-title")
                yield ListView(
                    ListItem(Label("Overview"), id="nav-overview"),
                    ListItem(Label("Leases"), id="nav-leases"),
                    id="shell-nav",
                )
            with Vertical(id="leases-panel"):
                yield Static("Pods / Leases", id="leases-title")
                yield Static("Loading pod leases", id="leases-summary", classes="summary")
                table: DataTable[str] = DataTable(id="leases-table")
                table.cursor_type = "row"
                table.zebra_stripes = True
                yield table
                yield Static("", id="leases-empty", classes="summary")
                yield Static("", id="leases-refreshed", classes="summary")
                yield Static("", id="leases-error", classes="error")
        yield Footer()

    async def on_mount(self) -> None:
        table = self._table()
        table.add_columns("Lease", "Pod", "Provider", "State", "Ready", "Expires", "Cost")
        await self._refresh()

    async def action_refresh(self) -> None:
        await self._refresh()

    async def action_show_leases(self) -> None:
        await self._refresh()

    async def action_show_overview(self) -> None:
        await self.app.switch_screen("overview")

    async def _refresh(self) -> None:
        self.query_one("#leases-error", Static).update("")
        self.query_one("#leases-empty", Static).update("")
        try:
            snapshot = await self._source.load_leases()
        except (
            Exception
        ) as exc:  # reason: TUI refresh must degrade to an inline error, never crash the app
            self._table().clear()
            self.query_one("#leases-summary", Static).update("0 active pod leases")
            self.query_one("#leases-refreshed", Static).update("")
            self.query_one("#leases-error", Static).update(
                source_failure_message("Pods / leases unavailable", exc)
            )
            return
        self._render_snapshot(snapshot)

    def _render_snapshot(self, snapshot: LeasesSnapshot) -> None:
        table = self._table()
        table.clear()
        for row in snapshot.rows:
            table.add_row(
                row.lease_id,
                row.pod_id,
                row.provider_id,
                row.state_label,
                row.readiness,
                row.expires_label,
                row.cost_label,
                key=row.lease_id,
            )

        self.query_one("#leases-summary", Static).update(
            f"{snapshot.active_count} active pod leases"
        )
        self.query_one("#leases-empty", Static).update(
            "No active pod leases" if not snapshot.rows else ""
        )
        self.query_one("#leases-refreshed", Static).update(
            f"Last refreshed: {snapshot.refreshed_label}"
        )

    def _table(self) -> DataTable[str]:
        return cast(DataTable[str], self.query_one("#leases-table", DataTable))


def _display_row_from_record(row: asyncpg.Record) -> LeaseDisplayRow:
    raw_readiness = row.get("readiness")
    readiness = raw_readiness if isinstance(raw_readiness, Mapping) else None
    return LeaseDisplayRow(
        lease_id=str(row["id"]),
        provider_id=str(row["provider_id"]),
        pod_id=str(row["runpod_pod_id"]),
        state=normalized_status(str(row["state"])),
        readiness=readiness_label(readiness),
        expires_at=_datetime_from_value(row["expires_at"], field_name="expires_at"),
        cost_accrued_usd=decimal_or_none(row.get("cost_accrued_usd")),
    )


def readiness_label(readiness: Mapping[str, object] | None) -> str:
    """Return a compact readiness label for a persisted readiness payload."""

    if readiness is None:
        return "pending"
    populated = {key for key in _READY_KEYS if bool(readiness.get(key))}
    if populated == _READY_KEYS:
        return "ready"
    if populated:
        return "partial"
    return "pending"


def normalized_status(value: str | None) -> str:
    """Return a display-safe status key."""

    if value is None:
        return "unknown"
    stripped = value.strip().lower()
    return stripped or "unknown"


def format_optional_usd(amount: Decimal | None) -> str:
    """Format an optional USD Decimal with cent rounding."""

    if amount is None:
        return "pending"
    rounded = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    return f"${rounded:,.2f}"


def decimal_or_none(value: object) -> Decimal | None:
    """Convert optional numeric DB payloads to Decimal."""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float | str):
        return Decimal(str(value))
    return None


def format_utc(value: dt.datetime) -> str:
    """Render a datetime as an operator-facing UTC label."""

    return as_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def as_utc(value: dt.datetime) -> dt.datetime:
    """Normalize a datetime for operator display."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def utc_now() -> dt.datetime:
    """Return current UTC time."""

    return dt.datetime.now(dt.UTC)


def _datetime_from_value(value: object, *, field_name: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value
    raise TypeError(f"{field_name} must be a datetime")


__all__ = [
    "LeaseDisplayRow",
    "LeasesScreen",
    "LeasesSnapshot",
    "LeasesSource",
    "PostgresLeasesSource",
    "StaticLeasesSource",
    "format_optional_usd",
    "normalized_status",
    "readiness_label",
]
