"""Hermetic chaos/kill-drill tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.api.admin.kill_switch import KillReport
from pitwall.ops.chaos_drill import (
    DRILL_TYPE,
    EXPECTED_KILL_STAGE_ORDER,
    ChaosCheckReport,
    FailureInjection,
    build_chaos_drill_report,
    run_chaos_drill,
    run_scheduled_chaos_drill,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class _StaticKillSwitch:
    def __init__(
        self,
        *,
        stage_order: list[str],
        total_duration_ms: int = 25,
        errors: list[str] | None = None,
    ) -> None:
        self.stage_order = stage_order
        self.total_duration_ms = total_duration_ms
        self.errors = errors or []
        self.reasons: list[str] = []

    async def activate(self, reason: str) -> KillReport:
        self.reasons.append(reason)
        return KillReport(
            triggered_at=NOW,
            reason=reason,
            tailscale_acl_updated=True,
            devices_removed=2,
            pods_terminated=0,
            total_duration_ms=self.total_duration_ms,
            errors=self.errors,
        )


@pytest.mark.anyio
@pytest.mark.chaos
async def test_dry_run_kill_drill_preserves_three_stage_order_without_termination() -> None:
    kill_switch = _StaticKillSwitch(stage_order=list(EXPECTED_KILL_STAGE_ORDER))

    report = await run_chaos_drill(
        reason="launch-gate dry run",
        kill_switch=kill_switch,
        failure_injections=(),
        started_at=NOW,
    )

    kill_check = report.checks[0]
    assert report.drill_type == DRILL_TYPE
    assert report.dry_run is True
    assert report.passed is True
    assert report.kill_stage_order == list(EXPECTED_KILL_STAGE_ORDER)
    assert kill_switch.reasons == ["launch-gate dry run"]
    assert kill_check.name == "dry_run_kill_switch"
    assert kill_check.passed is True
    assert kill_check.observations["pods_terminated"] == 0
    assert kill_check.observations["duration_budget_ms"] == 30_000


@pytest.mark.anyio
@pytest.mark.chaos
async def test_dry_run_kill_drill_fails_on_stage_order_regression() -> None:
    kill_switch = _StaticKillSwitch(
        stage_order=["device-removal", "acl-deny", "compute-termination"],
    )

    report = await run_chaos_drill(
        reason="stage order regression",
        kill_switch=kill_switch,
        failure_injections=(),
        started_at=NOW,
    )

    assert report.passed is False
    assert report.kill_stage_order == ["device-removal", "acl-deny", "compute-termination"]
    assert "kill-switch stage order mismatch" in report.errors[0]


@pytest.mark.anyio
@pytest.mark.chaos
async def test_dry_run_kill_drill_fails_when_duration_budget_exceeded() -> None:
    kill_switch = _StaticKillSwitch(
        stage_order=list(EXPECTED_KILL_STAGE_ORDER),
        total_duration_ms=30_001,
    )

    report = await run_chaos_drill(
        reason="duration budget regression",
        kill_switch=kill_switch,
        failure_injections=(),
        started_at=NOW,
    )

    assert report.passed is False
    assert any("duration budget exceeded" in error for error in report.errors)


@pytest.mark.anyio
@pytest.mark.chaos
async def test_failure_injections_pass_when_expected_provider_and_db_failures_are_contained() -> (
    None
):
    async def provider_failure() -> None:
        raise RuntimeError("runpod provider 500")

    async def db_failure() -> None:
        raise ConnectionError("postgres unreachable")

    report = await run_chaos_drill(
        reason="failure injection",
        failure_injections=(
            FailureInjection(
                name="provider_api_5xx",
                kind="provider_failure",
                operation=provider_failure,
                expected_exceptions=(RuntimeError,),
            ),
            FailureInjection(
                name="postgres_outage",
                kind="db_failure",
                operation=db_failure,
                expected_exceptions=(ConnectionError,),
            ),
        ),
        started_at=NOW,
    )

    provider_check = next(check for check in report.checks if check.name == "provider_api_5xx")
    db_check = next(check for check in report.checks if check.name == "postgres_outage")
    assert provider_check.passed is True
    assert provider_check.observations["safe_degradation"] == "expected_failure_contained"
    assert provider_check.observations["exception_type"] == "RuntimeError"
    assert db_check.passed is True
    assert db_check.observations["safe_degradation"] == "expected_failure_contained"
    assert db_check.observations["exception_type"] == "ConnectionError"


@pytest.mark.anyio
@pytest.mark.chaos
async def test_failure_injection_fails_when_probe_does_not_trigger_failure() -> None:
    async def provider_success() -> str:
        return "unexpected success"

    report = await run_chaos_drill(
        reason="missing failure",
        failure_injections=(
            FailureInjection(
                name="provider_api_5xx",
                kind="provider_failure",
                operation=provider_success,
                expected_exceptions=(RuntimeError,),
            ),
        ),
        started_at=NOW,
    )

    assert report.passed is False
    assert any("completed without expected failure" in error for error in report.errors)


@pytest.mark.anyio
@pytest.mark.chaos
async def test_chaos_drill_emits_structured_json_report(tmp_path: Path) -> None:
    report = await run_chaos_drill(
        reason="artifact report",
        failure_injections=(),
        artifact_output_dir=tmp_path,
        started_at=NOW,
    )

    assert report.artifact_path is not None
    artifact_path = Path(report.artifact_path)
    assert artifact_path.exists()
    body = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert body["drill_type"] == DRILL_TYPE
    assert body["reason"] == "artifact report"
    assert body["dry_run"] is True
    assert body["checks"][0]["name"] == "dry_run_kill_switch"


@pytest.mark.anyio
@pytest.mark.chaos
async def test_scheduled_chaos_drill_is_off_by_default(tmp_path: Path) -> None:
    report = await run_scheduled_chaos_drill(
        {},
        environ={},
        artifact_output_dir=tmp_path,
    )

    assert report is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.property
@given(
    check_passes=st.lists(st.booleans(), min_size=1, max_size=12),
    top_errors=st.lists(st.text(min_size=1, max_size=20), max_size=4),
)
def test_report_pass_status_is_merge_of_check_results_and_top_level_errors(
    check_passes: list[bool],
    top_errors: list[str],
) -> None:
    checks = [
        ChaosCheckReport(
            name=f"check_{index}",
            kind="custom_failure",
            passed=passed,
            duration_ms=index,
        )
        for index, passed in enumerate(check_passes)
    ]

    report = build_chaos_drill_report(
        reason="property",
        started_at=NOW,
        completed_at=NOW,
        dry_run=True,
        duration_budget_ms=30_000,
        kill_stage_order=list(EXPECTED_KILL_STAGE_ORDER),
        checks=checks,
        errors=top_errors,
    )

    assert report.passed is (all(check_passes) and not top_errors)
