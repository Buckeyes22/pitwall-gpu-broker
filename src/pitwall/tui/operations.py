"""Operations screen and read-only Initiative-2 state source for the Textual console."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Protocol, cast

import asyncpg
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from pitwall.autopilot import AutopilotController, AutopilotRunResult
from pitwall.core.models import Capability, Provider
from pitwall.cost import WhatIfSimulator
from pitwall.db.repository import CapabilityRepository, ProviderRepository
from pitwall.policy import (
    PolicyEvaluationResult,
    PolicySet,
    evaluate_policies,
    load_default_policy_set,
)
from pitwall.routing import PlanningContext, RoutingRequest, plan_route
from pitwall.tui.errors import source_failure_message
from pitwall.tui.overview import as_utc, format_count_summary, format_usd, utc_now

PoolFactory = Callable[[], Awaitable[asyncpg.Pool]]
CapabilityLoader = Callable[[asyncpg.Pool], Awaitable[Sequence[Capability]]]
ProviderLoader = Callable[[asyncpg.Pool], Awaitable[Sequence[Provider]]]
JobStateCountsLoader = Callable[[asyncpg.Pool], Awaitable[Mapping[str, int]]]
RecentJobsLoader = Callable[[asyncpg.Pool], Awaitable[Sequence["JobEntry"]]]
PolicyLoader = Callable[[], PolicySet]
AutopilotRunner = Callable[
    [dt.datetime, PolicySet, Sequence[Capability], Sequence[Provider]],
    AutopilotRunResult,
]

_CATALOG_TABLE_HEADER = ("Capability ID", "Name", "Class", "Cost", "Enabled", "Providers")
_JOBS_TABLE_HEADER = ("Job ID", "Capability", "Provider", "State", "Submitted", "Cost")
_RESILIENCE_TABLE_HEADER = ("Provider ID", "Health", "Failures", "Cooldown", "Error")


class OperationsSource(Protocol):
    """Async provider for a single Operations refresh."""

    async def load_operations(self) -> OperationsSnapshot:
        """Return the current Initiative-2 operations snapshot."""


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """Read-only capability catalog row rendered by the Operations screen."""

    capability_id: str
    name: str
    class_label: str
    cost_mode: str
    enabled: bool
    provider_count: int

    @property
    def enabled_label(self) -> str:
        return "enabled" if self.enabled else "disabled"

    def as_row(self) -> tuple[str, str, str, str, str, str]:
        """Return table cell values in display order."""

        return (
            display_text(self.capability_id),
            display_text(self.name),
            display_text(self.class_label),
            display_text(self.cost_mode),
            self.enabled_label,
            str(self.provider_count),
        )


@dataclass(frozen=True, slots=True)
class JobEntry:
    """Read-only recent workload row rendered by the Operations screen."""

    workload_id: str
    capability_id: str
    provider_id: str
    state: str
    submitted_at: dt.datetime
    cost_usd: Decimal | None
    workload_input: Mapping[str, object] = field(default_factory=dict)

    @property
    def submitted_label(self) -> str:
        submitted = as_utc(self.submitted_at)
        return submitted.strftime("%Y-%m-%d %H:%M UTC")

    @property
    def cost_label(self) -> str:
        if self.cost_usd is None:
            return "unknown"
        return format_usd(self.cost_usd)

    def as_row(self) -> tuple[str, str, str, str, str, str]:
        """Return table cell values in display order."""

        return (
            display_text(self.workload_id),
            display_text(self.capability_id),
            display_text(self.provider_id),
            display_text(self.state),
            self.submitted_label,
            self.cost_label,
        )

    def policy_payload(self) -> Mapping[str, object]:
        """Return a policy-evaluation payload without exposing rendered input values."""

        payload: dict[str, object] = dict(self.workload_input)
        payload.update(
            {
                "id": self.workload_id,
                "capability_id": self.capability_id,
                "provider_id": self.provider_id,
                "state": self.state,
                "submitted_at": self.submitted_at,
                "cost_usd": self.cost_usd,
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class ProviderResilienceEntry:
    """Provider health and cooldown row rendered by the Operations screen."""

    provider_id: str
    health: str
    consecutive_failures: int
    cooldown_trips: int
    cooldown_until: dt.datetime | None
    recent_error_rate: float

    @property
    def failures_label(self) -> str:
        return f"{self.consecutive_failures}/{self.cooldown_trips}"

    @property
    def cooldown_label(self) -> str:
        if self.cooldown_until is None:
            return "none"
        cooldown = as_utc(self.cooldown_until)
        return cooldown.strftime("%Y-%m-%d %H:%M UTC")

    @property
    def error_rate_label(self) -> str:
        return format_error_rate(self.recent_error_rate)

    @property
    def in_cooldown(self) -> bool:
        return self.cooldown_until is not None

    def as_row(self) -> tuple[str, str, str, str, str]:
        """Return table cell values in display order."""

        return (
            display_text(self.provider_id),
            display_text(self.health),
            self.failures_label,
            self.cooldown_label,
            self.error_rate_label,
        )


@dataclass(frozen=True, slots=True)
class RoutingSummary:
    """Summary of the representative deterministic route plan."""

    capability_name: str
    selected_provider_id: str | None
    fallback_chain: tuple[str, ...]
    candidate_count: int
    eliminated_count: int
    capacity_decision_count: int

    @property
    def fallback_label(self) -> str:
        fallbacks = tuple(
            provider_id
            for provider_id in self.fallback_chain
            if provider_id != self.selected_provider_id
        )
        if not fallbacks:
            return "none"
        return " -> ".join(fallbacks)

    @property
    def summary(self) -> str:
        selected = display_text(self.selected_provider_id, fallback="none")
        return (
            f"Routing: {display_text(self.capability_name)} -> {selected}"
            f" | fallbacks {self.fallback_label}"
            f" | candidates {self.candidate_count}"
            f" | dropped {self.eliminated_count}"
            f" | capacity {self.capacity_decision_count}"
        )


@dataclass(frozen=True, slots=True)
class PolicySummary:
    """Summary of the packaged policy-as-code evaluation."""

    policy_count: int
    decision: str
    violation_count: int

    @property
    def summary(self) -> str:
        return (
            f"Policies: {display_text(self.decision)}"
            f" | {_count_label(self.policy_count, 'policy')}"
            f" | {_count_label(self.violation_count, 'violation')}"
        )

    @classmethod
    def from_result(
        cls,
        *,
        policy_set: PolicySet,
        result: PolicyEvaluationResult,
    ) -> PolicySummary:
        return cls(
            policy_count=len(policy_set.policies),
            decision=result.decision,
            violation_count=len(result.violations),
        )


@dataclass(frozen=True, slots=True)
class AutopilotSummary:
    """Summary of one read-only Autopilot controller pass."""

    mode: str
    decision_count: int
    applied_count: int
    outcome_counts: Mapping[str, int]

    @property
    def summary(self) -> str:
        parts = [
            f"Autopilot: {display_text(self.mode)}",
            _count_label(self.decision_count, "decision"),
            f"{self.applied_count} applied",
        ]
        parts.extend(
            f"{key} {count}" for key, count in sorted(self.outcome_counts.items()) if count > 0
        )
        return " | ".join(parts)

    @classmethod
    def from_result(cls, result: AutopilotRunResult) -> AutopilotSummary:
        outcome_counts: dict[str, int] = {}
        for decision in result.decisions:
            outcome_counts[decision.outcome] = outcome_counts.get(decision.outcome, 0) + 1
        return cls(
            mode=_value_text(result.mode),
            decision_count=len(result.decisions),
            applied_count=result.applied_count,
            outcome_counts=outcome_counts,
        )


@dataclass(frozen=True, slots=True)
class OperationsSnapshot:
    """Read-only state rendered by the Operations screen."""

    catalog: tuple[CatalogEntry, ...]
    job_state_counts: Mapping[str, int]
    recent_jobs: tuple[JobEntry, ...]
    resilience: tuple[ProviderResilienceEntry, ...]
    routing: RoutingSummary
    policy: PolicySummary
    autopilot: AutopilotSummary
    refreshed_at: dt.datetime

    @property
    def summary(self) -> str:
        return (
            "Operations: "
            f"{_count_label(len(self.catalog), 'capability')} | "
            f"{_count_label(self.provider_total, 'provider')} | "
            f"{_count_label(self.job_total, 'job')} | "
            f"policies {self.policy.decision} | "
            f"autopilot {self.autopilot.mode}"
        )

    @property
    def provider_total(self) -> int:
        return sum(entry.provider_count for entry in self.catalog)

    @property
    def job_total(self) -> int:
        return sum(count for count in self.job_state_counts.values() if count > 0)

    @property
    def jobs_summary(self) -> str:
        return f"Jobs: {format_count_summary(self.job_state_counts)}"

    @property
    def resilience_summary(self) -> str:
        unhealthy = sum(1 for entry in self.resilience if entry.health.lower() == "unhealthy")
        cooldown = sum(1 for entry in self.resilience if entry.in_cooldown)
        return (
            f"Resilience: {_count_label(len(self.resilience), 'provider')}"
            f" | {unhealthy} unhealthy"
            f" | {cooldown} in cooldown"
        )

    @property
    def refreshed_label(self) -> str:
        refreshed = as_utc(self.refreshed_at)
        return refreshed.strftime("%Y-%m-%d %H:%M UTC")


class StaticOperationsSource:
    """Hermetic source used by tests and local demos."""

    def __init__(self, snapshot: OperationsSnapshot) -> None:
        self._snapshot = snapshot
        self.load_count = 0

    async def load_operations(self) -> OperationsSnapshot:
        self.load_count += 1
        return self._snapshot


class PostgresOperationsSource:
    """Read catalog, jobs, routing, policy, and autopilot state from existing layers."""

    def __init__(
        self,
        *,
        pool_factory: PoolFactory,
        capability_loader: CapabilityLoader | None = None,
        provider_loader: ProviderLoader | None = None,
        job_state_counts_loader: JobStateCountsLoader | None = None,
        recent_jobs_loader: RecentJobsLoader | None = None,
        policy_loader: PolicyLoader | None = None,
        autopilot_runner: AutopilotRunner | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._pool_factory = pool_factory
        self._capability_loader = capability_loader or _default_capability_loader
        self._provider_loader = provider_loader or _default_provider_loader
        self._job_state_counts_loader = job_state_counts_loader or _fetch_job_state_counts
        self._recent_jobs_loader = recent_jobs_loader or _fetch_recent_jobs
        self._policy_loader = policy_loader or load_default_policy_set
        self._autopilot_runner = autopilot_runner or _default_autopilot_runner
        self._now = now or utc_now

    async def load_operations(self) -> OperationsSnapshot:
        pool = await self._pool_factory()
        observed_at = as_utc(self._now())
        capabilities = tuple(await self._capability_loader(pool))
        providers = tuple(await self._provider_loader(pool))
        job_state_counts = dict(await self._job_state_counts_loader(pool))
        recent_jobs = tuple(await self._recent_jobs_loader(pool))

        policy_set = self._policy_loader()
        policy_result = evaluate_policies(
            policy_set,
            _OperationsPolicyConfig.from_state(
                capabilities=capabilities,
                providers=providers,
                recent_jobs=recent_jobs,
            ),
        )
        autopilot_result = self._autopilot_runner(
            observed_at,
            policy_set,
            capabilities,
            providers,
        )

        return OperationsSnapshot(
            catalog=_catalog_entries(capabilities, providers),
            job_state_counts=job_state_counts,
            recent_jobs=recent_jobs,
            resilience=_resilience_entries(providers),
            routing=_routing_summary(capabilities, providers, now=observed_at),
            policy=PolicySummary.from_result(policy_set=policy_set, result=policy_result),
            autopilot=AutopilotSummary.from_result(autopilot_result),
            refreshed_at=observed_at,
        )


class OperationsScreen(Screen[None]):
    """Read-only Operations screen in the operator console."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, source: OperationsSource) -> None:
        super().__init__(name="operations")
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
                    ListItem(Label("Operations"), id="nav-operations"),
                    id="shell-nav",
                )
            with Vertical(id="operations-panel"):
                yield Static("Operations", id="operations-title")
                yield Static("Loading operations", id="operations-summary", classes="summary")
                yield Static("", id="jobs-summary", classes="summary")
                yield Static("Catalog", classes="operation-section")
                yield Static("Loading catalog", id="catalog-table", classes="operation-table")
                yield Static("Jobs", classes="operation-section")
                yield Static("Loading jobs", id="jobs-table", classes="operation-table")
                yield Static("Resilience", classes="operation-section")
                yield Static(
                    "Loading resilience",
                    id="resilience-table",
                    classes="operation-table",
                )
                yield Static("", id="routing-summary", classes="summary")
                yield Static("", id="policy-summary", classes="summary")
                yield Static("", id="autopilot-summary", classes="summary")
                yield Static("", id="operations-refreshed", classes="summary")
                yield Static("", id="operations-error", classes="error")
        yield Footer()

    async def on_mount(self) -> None:
        await self._refresh()

    async def action_refresh(self) -> None:
        await self._refresh()

    async def _refresh(self) -> None:
        self.query_one("#operations-error", Static).update("")
        try:
            snapshot = await self._source.load_operations()
        except (
            Exception
        ) as exc:  # reason: TUI refresh must degrade to an inline error, never crash the app
            self.query_one("#operations-error", Static).update(
                source_failure_message("Operations unavailable", exc)
            )
            return
        self._render_snapshot(snapshot)

    def _render_snapshot(self, snapshot: OperationsSnapshot) -> None:
        self.query_one("#operations-summary", Static).update(snapshot.summary)
        self.query_one("#jobs-summary", Static).update(snapshot.jobs_summary)
        self.query_one("#catalog-table", Static).update(format_catalog_table(snapshot.catalog))
        self.query_one("#jobs-table", Static).update(format_jobs_table(snapshot.recent_jobs))
        self.query_one("#resilience-table", Static).update(
            format_resilience_table(snapshot.resilience)
        )
        self.query_one("#routing-summary", Static).update(snapshot.routing.summary)
        self.query_one("#policy-summary", Static).update(snapshot.policy.summary)
        self.query_one("#autopilot-summary", Static).update(snapshot.autopilot.summary)
        self.query_one("#operations-refreshed", Static).update(
            f"Last refreshed: {snapshot.refreshed_label}"
        )


@dataclass(frozen=True, slots=True)
class _OperationsPolicyConfig:
    capability: Mapping[str, object] | None
    providers: tuple[Mapping[str, object], ...]
    workloads: tuple[Mapping[str, object], ...]

    @classmethod
    def from_state(
        cls,
        *,
        capabilities: Sequence[Capability],
        providers: Sequence[Provider],
        recent_jobs: Sequence[JobEntry],
    ) -> _OperationsPolicyConfig:
        capability = (
            None
            if not capabilities
            else cast(
                Mapping[str, object], capabilities[0].model_dump(mode="python", by_alias=True)
            )
        )
        return cls(
            capability=capability,
            providers=tuple(
                cast(Mapping[str, object], provider.model_dump(mode="python", by_alias=True))
                for provider in providers
            ),
            workloads=tuple(job.policy_payload() for job in recent_jobs),
        )

    def provider_fixtures(self) -> tuple[Mapping[str, object], ...]:
        return self.providers


def display_text(value: object, *, fallback: str = "unknown") -> str:
    """Return a stripped non-empty label for operator display."""

    if value is None:
        return fallback
    label = str(value).strip()
    return label or fallback


def format_error_rate(value: float) -> str:
    """Format a bounded provider error rate."""

    rate = value if math.isfinite(value) else 0.0
    rate = max(0.0, min(1.0, rate))
    return f"{rate * 100:.1f}%"


def format_catalog_table(entries: tuple[CatalogEntry, ...]) -> str:
    """Render a stable fixed-width capability catalog table."""

    rows: list[tuple[str, str, str, str, str, str]] = [_CATALOG_TABLE_HEADER]
    rows.extend(entry.as_row() for entry in entries)
    return _format_table(rows)


def format_jobs_table(entries: tuple[JobEntry, ...]) -> str:
    """Render a stable fixed-width recent jobs table."""

    rows: list[tuple[str, str, str, str, str, str]] = [_JOBS_TABLE_HEADER]
    rows.extend(entry.as_row() for entry in entries)
    return _format_table(rows)


def format_resilience_table(entries: tuple[ProviderResilienceEntry, ...]) -> str:
    """Render a stable fixed-width provider resilience table."""

    rows: list[tuple[str, str, str, str, str]] = [_RESILIENCE_TABLE_HEADER]
    rows.extend(entry.as_row() for entry in entries)
    return _format_table(rows)


async def _default_capability_loader(pool: asyncpg.Pool) -> Sequence[Capability]:
    return await CapabilityRepository(pool).list(limit=10_000)


async def _default_provider_loader(pool: asyncpg.Pool) -> Sequence[Provider]:
    return await ProviderRepository(pool).list(limit=10_000)


async def _fetch_job_state_counts(pool: asyncpg.Pool) -> Mapping[str, int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT state, COUNT(*) AS count
               FROM pitwall.workloads
               GROUP BY state
               ORDER BY state"""
        )
    return {display_text(row["state"]): int(row["count"]) for row in rows}


async def _fetch_recent_jobs(pool: asyncpg.Pool) -> Sequence[JobEntry]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, capability_id, provider_id, state, submitted_at,
                      COALESCE(cost_actual_usd, cost_estimate_usd) AS cost_usd,
                      input
               FROM pitwall.workloads
               ORDER BY submitted_at DESC, id DESC
               LIMIT $1""",
            8,
        )
    return tuple(_job_entry_from_row(row) for row in rows)


def _job_entry_from_row(row: asyncpg.Record) -> JobEntry:
    return JobEntry(
        workload_id=display_text(row["id"]),
        capability_id=display_text(row["capability_id"]),
        provider_id=display_text(row["provider_id"]),
        state=display_text(row["state"]),
        submitted_at=cast(dt.datetime, row["submitted_at"]),
        cost_usd=_decimal_or_none(row["cost_usd"]),
        workload_input=_mapping_or_empty(row["input"]),
    )


def _default_autopilot_runner(
    now: dt.datetime,
    policy_set: PolicySet,
    capabilities: Sequence[Capability],
    providers: Sequence[Provider],
) -> AutopilotRunResult:
    capability = capabilities[0] if capabilities else None
    context = PlanningContext.replay(
        now=now,
        providers=providers,
        capability=capability,
    )
    simulator = WhatIfSimulator(context)
    controller = AutopilotController(policy_set=policy_set, simulator=simulator)
    return controller.run(now=now)


def _catalog_entries(
    capabilities: Sequence[Capability],
    providers: Sequence[Provider],
) -> tuple[CatalogEntry, ...]:
    counts: dict[str, int] = {}
    for provider in providers:
        counts[provider.capability_id] = counts.get(provider.capability_id, 0) + 1
    return tuple(
        CatalogEntry(
            capability_id=capability.id,
            name=capability.name,
            class_label=_value_text(capability.class_),
            cost_mode=_value_text(capability.cost_mode),
            enabled=capability.enabled,
            provider_count=counts.get(capability.id, 0),
        )
        for capability in capabilities
    )


def _resilience_entries(providers: Sequence[Provider]) -> tuple[ProviderResilienceEntry, ...]:
    return tuple(
        ProviderResilienceEntry(
            provider_id=provider.id,
            health=display_text(provider.health_status),
            consecutive_failures=provider.consecutive_failures,
            cooldown_trips=provider.cooldown_trips,
            cooldown_until=provider.cooldown_until,
            recent_error_rate=provider.recent_error_rate,
        )
        for provider in providers
    )


def _routing_summary(
    capabilities: Sequence[Capability],
    providers: Sequence[Provider],
    *,
    now: dt.datetime,
) -> RoutingSummary:
    for capability in capabilities:
        capability_providers = tuple(
            provider for provider in providers if provider.capability_id == capability.id
        )
        if not capability_providers:
            continue
        try:
            plan = plan_route(
                RoutingRequest(
                    capability_name=capability.name,
                    capability_id=capability.id,
                ),
                capability_providers,
                capability=capability,
                now=now,
            )
        except ValueError:
            return RoutingSummary(
                capability_name=capability.name,
                selected_provider_id=None,
                fallback_chain=(),
                candidate_count=0,
                eliminated_count=0,
                capacity_decision_count=0,
            )
        return RoutingSummary(
            capability_name=capability.name,
            selected_provider_id=plan.selected_provider_id,
            fallback_chain=plan.fallback_chain,
            candidate_count=len(plan.ranked_candidates),
            eliminated_count=len(plan.eliminated),
            capacity_decision_count=len(plan.capacity_decisions),
        )

    return RoutingSummary(
        capability_name="none",
        selected_provider_id=None,
        fallback_chain=(),
        candidate_count=0,
        eliminated_count=0,
        capacity_decision_count=0,
    )


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _value_text(value: object) -> str:
    if isinstance(value, Enum):
        return display_text(value.value)
    return display_text(value)


def _count_label(count: int, singular: str) -> str:
    if count == 1:
        return f"{count} {singular}"
    if singular.endswith("y"):
        return f"{count} {singular[:-1]}ies"
    return f"{count} {singular}s"


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
    "AutopilotSummary",
    "CatalogEntry",
    "JobEntry",
    "OperationsScreen",
    "OperationsSnapshot",
    "OperationsSource",
    "PolicySummary",
    "PostgresOperationsSource",
    "ProviderResilienceEntry",
    "RoutingSummary",
    "StaticOperationsSource",
    "display_text",
    "format_catalog_table",
    "format_error_rate",
    "format_jobs_table",
    "format_resilience_table",
]
