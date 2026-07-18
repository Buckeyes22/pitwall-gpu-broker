"""Automated chaos and dry-run kill-drill harness.

The harness is intentionally hermetic by default: it records the kill-switch
contract order with a dry-run implementation and contains simulated provider
and database failures as report evidence instead of touching live services.
"""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from pitwall.api.admin.kill_switch import DEFAULT_TAG, KillReport
from pitwall.db.drill_evidence import write_drill_json_report

DRILL_TYPE = "chaos_kill_drill"
DEFAULT_DURATION_BUDGET_MS = 30_000
CHAOS_DRILL_ENABLED_ENV = "PITWALL_CHAOS_DRILL_ENABLED"
EXPECTED_KILL_STAGE_ORDER: tuple[str, str, str] = (
    "acl-deny",
    "device-removal",
    "compute-termination",
)

_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}

CheckKind = Literal["kill_switch", "provider_failure", "db_failure", "custom_failure"]
FailureOperation = Callable[[], Awaitable[object]]


class DryRunKillSwitch(Protocol):
    """Protocol for a kill switch implementation safe to invoke in a drill."""

    @property
    def stage_order(self) -> Sequence[str]:
        """Observed stage order from the most recent activation."""
        ...

    async def activate(self, reason: str) -> KillReport:
        """Return a kill report without terminating real resources."""
        ...


@dataclass(frozen=True)
class FailureInjection:
    """A simulated failure operation that should be safely contained."""

    name: str
    kind: Literal["provider_failure", "db_failure", "custom_failure"]
    operation: FailureOperation
    expected_exceptions: tuple[type[BaseException], ...] = (Exception,)


class ChaosCheckReport(BaseModel):
    """Result of one chaos drill check."""

    name: str
    kind: CheckKind
    passed: bool
    duration_ms: int
    observations: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class ChaosDrillReport(BaseModel):
    """Structured report emitted by the chaos drill runner."""

    drill_id: str
    drill_type: str
    reason: str
    started_at: datetime
    completed_at: datetime
    dry_run: bool
    passed: bool
    duration_budget_ms: int
    kill_stage_order: list[str]
    checks: list[ChaosCheckReport]
    errors: list[str] = Field(default_factory=list)
    artifact_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class DryRunCloudKillSwitch:
    """Dry-run implementation of the CloudKillSwitch three-stage contract."""

    def __init__(self, *, tag: str = DEFAULT_TAG) -> None:
        self.tag = tag
        self.stage_order: list[str] = []

    async def activate(self, reason: str) -> KillReport:
        if not reason:
            raise ValueError("reason is required (audit trail)")

        started = time.perf_counter()
        self.stage_order.append("acl-deny")
        self.stage_order.append("device-removal")
        self.stage_order.append("compute-termination")
        duration_ms = int((time.perf_counter() - started) * 1000)
        return KillReport(
            triggered_at=datetime.now(UTC),
            reason=reason,
            tailscale_acl_updated=True,
            devices_removed=0,
            pods_terminated=0,
            total_duration_ms=duration_ms,
            errors=[],
        )


def chaos_drill_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return True only when the scheduled chaos drill is explicitly enabled."""

    env = os.environ if environ is None else environ
    return env.get(CHAOS_DRILL_ENABLED_ENV, "").strip().lower() in _TRUE_ENV_VALUES


def build_chaos_drill_report(
    *,
    reason: str,
    started_at: datetime,
    completed_at: datetime,
    dry_run: bool,
    duration_budget_ms: int,
    kill_stage_order: Sequence[str],
    checks: Sequence[ChaosCheckReport],
    errors: Sequence[str],
    artifact_path: str | None = None,
) -> ChaosDrillReport:
    """Build the final report, merging check failures with top-level errors."""

    checks_list = list(checks)
    errors_list = list(errors)
    passed = all(check.passed for check in checks_list) and not errors_list
    return ChaosDrillReport(
        drill_id=f"{DRILL_TYPE}-{started_at.strftime('%Y%m%d-%H%M%S')}",
        drill_type=DRILL_TYPE,
        reason=reason,
        started_at=started_at,
        completed_at=completed_at,
        dry_run=dry_run,
        passed=passed,
        duration_budget_ms=duration_budget_ms,
        kill_stage_order=list(kill_stage_order),
        checks=checks_list,
        errors=errors_list,
        artifact_path=artifact_path,
    )


def emit_chaos_drill_report(
    report: ChaosDrillReport,
    *,
    output_dir: str | Path | None = None,
) -> Path:
    """Write a chaos drill report JSON artifact and return its path."""

    return write_drill_json_report(
        report.to_dict(),
        drill_type=DRILL_TYPE,
        output_dir=output_dir,
    )


async def _simulated_provider_failure() -> object:
    raise RuntimeError("simulated provider failure")


async def _simulated_db_failure() -> object:
    raise ConnectionError("simulated database failure")


def default_failure_injections() -> tuple[FailureInjection, FailureInjection]:
    """Return the default hermetic provider and database failure probes."""

    return (
        FailureInjection(
            name="simulated_provider_failure",
            kind="provider_failure",
            operation=_simulated_provider_failure,
            expected_exceptions=(RuntimeError,),
        ),
        FailureInjection(
            name="simulated_db_failure",
            kind="db_failure",
            operation=_simulated_db_failure,
            expected_exceptions=(ConnectionError,),
        ),
    )


async def _run_kill_switch_check(
    *,
    reason: str,
    kill_switch: DryRunKillSwitch,
    duration_budget_ms: int,
) -> ChaosCheckReport:
    started = time.perf_counter()
    try:
        kill_report = await kill_switch.activate(reason)
    except (
        Exception
    ) as exc:  # reason: drill captures any kill-switch failure as a report, never crashes
        duration_ms = int((time.perf_counter() - started) * 1000)
        return ChaosCheckReport(
            name="dry_run_kill_switch",
            kind="kill_switch",
            passed=False,
            duration_ms=duration_ms,
            observations={},
            errors=[f"kill-switch dry-run raised {type(exc).__name__}: {exc}"],
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    stage_order = list(kill_switch.stage_order)
    expected_order = list(EXPECTED_KILL_STAGE_ORDER)
    errors: list[str] = []
    if stage_order != expected_order:
        errors.append(
            f"kill-switch stage order mismatch: expected {expected_order}, observed {stage_order}"
        )
    if kill_report.total_duration_ms > duration_budget_ms:
        errors.append(
            "kill-switch duration budget exceeded: "
            f"{kill_report.total_duration_ms}ms > {duration_budget_ms}ms"
        )
    if kill_report.pods_terminated != 0:
        errors.append(
            f"dry-run kill switch terminated compute: pods_terminated={kill_report.pods_terminated}"
        )
    for error in kill_report.errors:
        errors.append(f"kill-switch reported error: {error}")

    return ChaosCheckReport(
        name="dry_run_kill_switch",
        kind="kill_switch",
        passed=not errors,
        duration_ms=duration_ms,
        observations={
            "dry_run": True,
            "stage_order": stage_order,
            "expected_stage_order": expected_order,
            "tailscale_acl_updated": kill_report.tailscale_acl_updated,
            "devices_removed": kill_report.devices_removed,
            "pods_terminated": kill_report.pods_terminated,
            "reported_duration_ms": kill_report.total_duration_ms,
            "duration_budget_ms": duration_budget_ms,
        },
        errors=errors,
    )


async def _run_failure_injection(injection: FailureInjection) -> ChaosCheckReport:
    started = time.perf_counter()
    try:
        result = await injection.operation()
    except injection.expected_exceptions as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return ChaosCheckReport(
            name=injection.name,
            kind=injection.kind,
            passed=True,
            duration_ms=duration_ms,
            observations={
                "safe_degradation": "expected_failure_contained",
                "exception_type": type(exc).__name__,
                "message": str(exc),
            },
        )
    except Exception as exc:  # reason: drill captures any check failure as a report, never crashes
        duration_ms = int((time.perf_counter() - started) * 1000)
        return ChaosCheckReport(
            name=injection.name,
            kind=injection.kind,
            passed=False,
            duration_ms=duration_ms,
            observations={
                "safe_degradation": "unexpected_failure_type",
                "exception_type": type(exc).__name__,
                "message": str(exc),
            },
            errors=[
                f"{injection.name} raised unexpected {type(exc).__name__}: {exc}",
            ],
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    return ChaosCheckReport(
        name=injection.name,
        kind=injection.kind,
        passed=False,
        duration_ms=duration_ms,
        observations={
            "safe_degradation": "missing_expected_failure",
            "result_type": type(result).__name__,
        },
        errors=[f"{injection.name} completed without expected failure"],
    )


async def run_chaos_drill(
    *,
    reason: str,
    kill_switch: DryRunKillSwitch | None = None,
    failure_injections: Sequence[FailureInjection] | None = None,
    duration_budget_ms: int = DEFAULT_DURATION_BUDGET_MS,
    artifact_output_dir: str | Path | None = None,
    started_at: datetime | None = None,
) -> ChaosDrillReport:
    """Run a hermetic dry-run kill drill plus simulated failure probes."""

    if not reason:
        raise ValueError("reason is required (audit trail)")

    drill_started_at = started_at or datetime.now(UTC)
    active_kill_switch: DryRunKillSwitch = (
        kill_switch if kill_switch is not None else DryRunCloudKillSwitch()
    )
    checks: list[ChaosCheckReport] = [
        await _run_kill_switch_check(
            reason=reason,
            kill_switch=active_kill_switch,
            duration_budget_ms=duration_budget_ms,
        )
    ]

    active_injections = (
        default_failure_injections() if failure_injections is None else tuple(failure_injections)
    )
    for injection in active_injections:
        checks.append(await _run_failure_injection(injection))

    top_level_errors = [error for check in checks for error in check.errors]
    completed_at = datetime.now(UTC)
    report = build_chaos_drill_report(
        reason=reason,
        started_at=drill_started_at,
        completed_at=completed_at,
        dry_run=True,
        duration_budget_ms=duration_budget_ms,
        kill_stage_order=list(active_kill_switch.stage_order),
        checks=checks,
        errors=top_level_errors,
    )
    if artifact_output_dir is not None:
        artifact_path = emit_chaos_drill_report(report, output_dir=artifact_output_dir)
        report = report.model_copy(update={"artifact_path": str(artifact_path)})
    return report


async def run_scheduled_chaos_drill(
    ctx: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    artifact_output_dir: str | Path | None = None,
    reason: str = "scheduled chaos drill",
) -> ChaosDrillReport | None:
    """Run the chaos drill only when PITWALL_CHAOS_DRILL_ENABLED is truthy."""

    if not chaos_drill_enabled(environ):
        return None

    output_dir = artifact_output_dir
    if output_dir is None:
        ctx_output_dir = ctx.get("chaos_drill_output_dir")
        if isinstance(ctx_output_dir, str | Path):
            output_dir = ctx_output_dir

    return await run_chaos_drill(
        reason=reason,
        artifact_output_dir=output_dir,
    )


__all__ = [
    "CHAOS_DRILL_ENABLED_ENV",
    "DEFAULT_DURATION_BUDGET_MS",
    "DRILL_TYPE",
    "EXPECTED_KILL_STAGE_ORDER",
    "ChaosCheckReport",
    "ChaosDrillReport",
    "DryRunCloudKillSwitch",
    "DryRunKillSwitch",
    "FailureInjection",
    "build_chaos_drill_report",
    "chaos_drill_enabled",
    "default_failure_injections",
    "emit_chaos_drill_report",
    "run_chaos_drill",
    "run_scheduled_chaos_drill",
]
