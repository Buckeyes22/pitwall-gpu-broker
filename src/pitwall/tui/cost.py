"""Cost screen and state sources for the Textual console."""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Protocol

import asyncpg
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from pitwall.cost.simulator import WhatIfBatchProjection
from pitwall.cost.sub_budgets import (
    ChargebackLineItem,
    ChargebackReport,
    SubBudgetConfig,
    generate_chargeback_report,
)
from pitwall.finops.burn_rate import BurnRateForecast, forecast_from_cost_daily
from pitwall.tui.errors import source_failure_message
from pitwall.tui.overview import as_utc, format_usd, utc_now

PoolFactory = Callable[[], Awaitable[asyncpg.Pool]]
_DAY_QUANTUM = Decimal("0.1")
_PERCENT_QUANTUM = Decimal("0.1")
_USD_QUANTUM = Decimal("0.000001")
_COST_TABLE_HEADER = ("Tag", "Allocation", "Spend", "Remaining", "Used")
_ADMITTED_STATES = ("queued", "running", "completed")


class CostSource(Protocol):
    """Async provider for a single Cost refresh."""

    async def load_cost(self) -> CostSnapshot:
        """Return the current cost analytics snapshot."""


@dataclass(frozen=True, slots=True)
class CostSnapshot:
    """Read-only state rendered by the Cost screen."""

    runway: BurnRateForecast
    chargeback: ChargebackReport
    what_if: WhatIfBatchProjection
    refreshed_at: dt.datetime

    @property
    def runway_summary(self) -> str:
        return (
            f"Burn: {format_usd(self.runway.burn_rate_usd_per_day)}/day"
            f" | Remaining: {format_usd(self.runway.remaining_budget_usd)}"
            f" | Runway: {format_days(self.runway.runway_days)}"
            f" | Trend: {self.runway.trend}"
            f" | Confidence: {format_percent(self.runway.confidence, Decimal('1'))}"
        )

    @property
    def sub_budget_summary(self) -> str:
        count = len(self.chargeback.line_items)
        noun = "tag" if count == 1 else "tags"
        return (
            f"Sub-budgets: {format_usd(self.chargeback.total_spend_usd)} spend "
            f"across {count} {noun} | "
            f"{format_usd(self.chargeback.unallocated_spend_usd)} unallocated"
        )

    @property
    def what_if_summary(self) -> str:
        return (
            f"What-if: reserves {format_usd(self.what_if.total_reserved_usd)}"
            f" | projected spend {format_usd(self.what_if.projected_spend_usd)}"
            f" | headroom {format_optional_usd(self.what_if.budget_headroom_usd)}"
            f" | {format_budget_status(self.what_if.would_exceed_budget)}"
        )

    @property
    def refreshed_label(self) -> str:
        refreshed = as_utc(self.refreshed_at)
        return refreshed.strftime("%Y-%m-%d %H:%M UTC")


class StaticCostSource:
    """Hermetic source used by tests and local demos."""

    def __init__(self, snapshot: CostSnapshot) -> None:
        self._snapshot = snapshot
        self.load_count = 0

    async def load_cost(self) -> CostSnapshot:
        self.load_count += 1
        return self._snapshot


class PostgresCostSource:
    """Read Cost view state from Pitwall's Postgres-backed cost layers."""

    def __init__(
        self,
        *,
        pool_factory: PoolFactory,
        now: Callable[[], dt.datetime] | None = None,
        monthly_budget_usd: Decimal | str | int | None = None,
        sub_budget_config: SubBudgetConfig | None = None,
        what_if_projection: WhatIfBatchProjection | None = None,
        window_days: int = 30,
    ) -> None:
        self._pool_factory = pool_factory
        self._now = now or utc_now
        self._monthly_budget_usd = _optional_positive_usd(monthly_budget_usd)
        self._sub_budget_config = sub_budget_config
        self._what_if_projection = what_if_projection
        self._window_days = window_days

    async def load_cost(self) -> CostSnapshot:
        pool = await self._pool_factory()
        observed_at = as_utc(self._now())
        budget = self._resolve_monthly_budget()
        mtd_spend = await _fetch_month_spend(pool)

        runway = await forecast_from_cost_daily(
            pool,
            budget_usd=budget,
            mtd_spend_usd=mtd_spend,
            now=observed_at,
            window_days=self._window_days,
        )
        workloads = await _fetch_month_workload_costs(pool)
        sub_budget_config = self._sub_budget_config or SubBudgetConfig(total_budget_usd=budget)
        chargeback = generate_chargeback_report(
            sub_budget_config,
            workloads,
            tag_resolver=resolve_workload_tag,
        )
        what_if = self._what_if_projection or empty_what_if_projection(
            starting_spend_usd=mtd_spend,
            budget_usd=budget,
        )

        return CostSnapshot(
            runway=runway,
            chargeback=chargeback,
            what_if=what_if,
            refreshed_at=observed_at,
        )

    def _resolve_monthly_budget(self) -> Decimal:
        if self._monthly_budget_usd is not None:
            return self._monthly_budget_usd
        return _env_positive_usd("PITWALL_MONTHLY_BUDGET_USD")


class CostScreen(Screen[None]):
    """Read-only Cost screen in the operator console."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, source: CostSource) -> None:
        super().__init__(name="cost")
        self._source = source

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="shell"):
            with Vertical(id="nav-panel"):
                yield Label("Pitwall", id="nav-title")
                yield ListView(
                    ListItem(Label("Overview"), id="nav-overview"),
                    ListItem(Label("Providers"), id="nav-providers"),
                    ListItem(Label("Cost"), id="nav-cost"),
                    id="shell-nav",
                )
            with Vertical(id="cost-panel"):
                yield Static("Cost", id="cost-title")
                yield Static("Loading runway", id="runway-summary", classes="summary")
                yield Static("", id="sub-budget-summary", classes="summary")
                yield Static("Loading sub-budgets", id="sub-budget-table")
                yield Static("", id="what-if-summary", classes="summary")
                yield Static("", id="cost-refreshed", classes="summary")
                yield Static("", id="cost-error", classes="error")
        yield Footer()

    async def on_mount(self) -> None:
        await self._refresh()

    async def action_refresh(self) -> None:
        await self._refresh()

    async def _refresh(self) -> None:
        self.query_one("#cost-error", Static).update("")
        try:
            snapshot = await self._source.load_cost()
        except (
            Exception
        ) as exc:  # reason: TUI refresh must degrade to an inline error, never crash the app
            self.query_one("#cost-error", Static).update(
                source_failure_message("Cost unavailable", exc)
            )
            return
        self._render_snapshot(snapshot)

    def _render_snapshot(self, snapshot: CostSnapshot) -> None:
        self.query_one("#runway-summary", Static).update(snapshot.runway_summary)
        self.query_one("#sub-budget-summary", Static).update(snapshot.sub_budget_summary)
        self.query_one("#sub-budget-table", Static).update(
            format_cost_table(snapshot.chargeback.line_items)
        )
        self.query_one("#what-if-summary", Static).update(snapshot.what_if_summary)
        self.query_one("#cost-refreshed", Static).update(
            f"Last refreshed: {snapshot.refreshed_label}"
        )


def format_cost_table(entries: tuple[ChargebackLineItem, ...]) -> str:
    """Render a stable fixed-width sub-budget table."""

    rows: list[tuple[str, str, str, str, str]] = [_COST_TABLE_HEADER]
    rows.extend(_chargeback_row(entry) for entry in entries)
    widths = [max(len(row[column]) for row in rows) for column in range(5)]

    rendered: list[str] = []
    for index, row in enumerate(rows):
        rendered.append(_format_row(row, widths))
        if index == 0:
            rendered.append("  ".join("-" * width for width in widths))
    return "\n".join(rendered)


def format_days(value: Decimal | None) -> str:
    """Format an optional runway duration."""

    if value is None:
        return "unavailable"
    rounded = value.quantize(_DAY_QUANTUM, rounding=ROUND_HALF_UP)
    noun = "day" if rounded == Decimal("1.0") else "days"
    return f"{rounded:.1f} {noun}"


def format_percent(numerator: Decimal, denominator: Decimal) -> str:
    """Format a bounded percentage for operator summaries."""

    if denominator <= 0:
        return "n/a"
    percent = (numerator / denominator * Decimal("100")).quantize(
        _PERCENT_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    if percent < 0:
        percent = Decimal("0.0")
    elif percent > 100:
        percent = Decimal("100.0")
    return f"{percent:.1f}%"


def format_optional_usd(value: Decimal | None) -> str:
    """Format an optional USD value."""

    if value is None:
        return "unavailable"
    return format_usd(value)


def format_budget_status(value: bool | None) -> str:
    """Format optional what-if budget status."""

    if value is None:
        return "budget unknown"
    if value:
        return "over budget"
    return "within budget"


def empty_what_if_projection(
    *,
    starting_spend_usd: Decimal,
    budget_usd: Decimal | None,
) -> WhatIfBatchProjection:
    """Return a read-only empty simulator summary when no what-if inputs are configured."""

    headroom = None if budget_usd is None else budget_usd - starting_spend_usd
    return WhatIfBatchProjection(
        projections=(),
        total_reserved_usd=Decimal("0.000000"),
        starting_spend_usd=starting_spend_usd,
        projected_spend_usd=starting_spend_usd,
        budget_usd=budget_usd,
        budget_headroom_usd=headroom,
        would_exceed_budget=None if headroom is None else headroom < 0,
    )


def resolve_workload_tag(workload: Mapping[str, Any]) -> str | None:
    """Resolve a sub-budget tag from a workload row mapping."""

    direct = _first_text_value(workload, ("budget_tag", "tag", "team"))
    if direct is not None:
        return direct

    raw_input = workload.get("input")
    if isinstance(raw_input, Mapping):
        return _first_text_value(raw_input, ("budget_tag", "tag", "team"))
    return None


async def _fetch_month_spend(pool: asyncpg.Pool) -> Decimal:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT COALESCE(SUM(COALESCE(cost_actual_usd, cost_estimate_usd, 0)), 0) AS s
               FROM pitwall.workloads
               WHERE submitted_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
                 AND state = ANY($1::text[])""",
            list(_ADMITTED_STATES),
        )
    if row is None:
        return Decimal("0.000000")
    return _usd(row["s"])


async def _fetch_month_workload_costs(pool: asyncpg.Pool) -> tuple[Mapping[str, Any], ...]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT input, cost_actual_usd, cost_estimate_usd
               FROM pitwall.workloads
               WHERE submitted_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
                 AND state = ANY($1::text[])
               ORDER BY submitted_at ASC, id ASC""",
            list(_ADMITTED_STATES),
        )
    return tuple(
        {
            "input": row["input"],
            "cost_actual_usd": row["cost_actual_usd"],
            "cost_estimate_usd": row["cost_estimate_usd"],
        }
        for row in rows
    )


def _chargeback_row(entry: ChargebackLineItem) -> tuple[str, str, str, str, str]:
    return (
        entry.tag,
        format_usd(entry.allocation_usd),
        format_usd(entry.spend_usd),
        format_usd(entry.remaining_usd),
        format_percent(entry.spend_usd, entry.allocation_usd),
    )


def _format_row(row: tuple[str, str, str, str, str], widths: list[int]) -> str:
    return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))


def _first_text_value(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        raw_value = mapping.get(key)
        if isinstance(raw_value, str):
            value = raw_value.strip()
            if value:
                return value
    return None


def _optional_positive_usd(value: Decimal | str | int | None) -> Decimal | None:
    if value is None:
        return None
    parsed = _usd(value)
    if parsed <= 0:
        raise ValueError("monthly_budget_usd must be positive")
    return parsed


def _env_positive_usd(name: str) -> Decimal:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} must be set")
    parsed = _usd(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _usd(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("USD value must be decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("USD value must be decimal") from exc
    if not parsed.is_finite():
        raise ValueError("USD value must be finite")
    return parsed.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)


__all__ = [
    "CostScreen",
    "CostSnapshot",
    "CostSource",
    "PostgresCostSource",
    "StaticCostSource",
    "empty_what_if_projection",
    "format_budget_status",
    "format_cost_table",
    "format_days",
    "format_optional_usd",
    "format_percent",
    "resolve_workload_tag",
]
