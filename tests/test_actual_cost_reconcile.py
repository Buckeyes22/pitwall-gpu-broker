"""Actual cost reconciliation — map terminal RunPod states and cost.

Tests for:
  - map_runpod_status: RunPod queue status → Pitwall RunPodJobStatus
  - apply_terminal_state: DB UPDATE of state + cost_actual_usd + completed_at
  - fetch_active_workloads + _cost_reconcile wiring
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"
_TEST_POSTGRES_CONTAINER = "pitwall-test-postgres"

_CAPABILITIES_SQL = (_MIGRATION_DIR / "0001_capabilities.sql").read_text()
_PROVIDERS_SQL = (_MIGRATION_DIR / "0002_providers.sql").read_text()
_WORKLOADS_SQL = (_MIGRATION_DIR / "0003_workloads.sql").read_text()
_COST_COLUMNS_SQL = (_MIGRATION_DIR / "0011_workload_cost_columns.sql").read_text()


class TestMapRunPodStatus:
    def test_completed_is_terminal(self) -> None:
        from pitwall.reconciler import map_runpod_status

        result = map_runpod_status("COMPLETED")
        assert result.terminal is True
        assert result.state is not None
        assert result.state.value == "completed"

    def test_failed_is_terminal(self) -> None:
        from pitwall.reconciler import map_runpod_status

        result = map_runpod_status("FAILED")
        assert result.terminal is True
        assert result.state is not None
        assert result.state.value == "failed"

    def test_cancelled_is_terminal(self) -> None:
        from pitwall.reconciler import map_runpod_status

        result = map_runpod_status("CANCELLED")
        assert result.terminal is True
        assert result.state is not None
        assert result.state.value == "cancelled"

    def test_in_queue_is_not_terminal(self) -> None:
        from pitwall.reconciler import map_runpod_status

        result = map_runpod_status("IN_QUEUE")
        assert result.terminal is False
        assert result.state is None

    def test_in_progress_is_not_terminal(self) -> None:
        from pitwall.reconciler import map_runpod_status

        result = map_runpod_status("IN_PROGRESS")
        assert result.terminal is False
        assert result.state is None

    def test_unknown_status_is_not_terminal(self) -> None:
        from pitwall.reconciler import map_runpod_status

        result = map_runpod_status("SOMETHING_WEIRD")
        assert result.terminal is False

    def test_terminal_state_has_completed_at_default(self) -> None:
        from pitwall.reconciler import map_runpod_status

        before = dt.datetime.now(dt.UTC)
        result = map_runpod_status("COMPLETED")
        after = dt.datetime.now(dt.UTC)
        assert result.completed_at is not None
        assert before <= result.completed_at <= after

    def test_terminal_state_uses_provided_completed_at(self) -> None:
        from pitwall.reconciler import map_runpod_status

        ts = dt.datetime(2025, 1, 15, 10, 30, 0, tzinfo=dt.UTC)
        result = map_runpod_status("COMPLETED", completed_at=ts)
        assert result.completed_at == ts

    def test_actual_cost_with_cost_per_hr_and_worker_time(self) -> None:
        from pitwall.reconciler import map_runpod_status

        cost_per_hr = Decimal("0.80")
        worker_time_ms = 3_600_000  # 1 hour
        result = map_runpod_status(
            "COMPLETED",
            cost_per_hr=cost_per_hr,
            worker_time_ms=worker_time_ms,
        )
        assert result.terminal is True
        assert result.actual_cost is not None
        assert result.actual_cost == Decimal("0.800000")

    def test_actual_cost_zero_worker_time(self) -> None:
        from pitwall.reconciler import map_runpod_status

        result = map_runpod_status(
            "COMPLETED",
            cost_per_hr=Decimal("0.80"),
            worker_time_ms=0,
        )
        assert result.actual_cost is None

    def test_actual_cost_missing_cost_per_hr(self) -> None:
        from pitwall.reconciler import map_runpod_status

        result = map_runpod_status(
            "COMPLETED",
            worker_time_ms=5000,
        )
        assert result.actual_cost is None

    def test_actual_cost_missing_worker_time(self) -> None:
        from pitwall.reconciler import map_runpod_status

        result = map_runpod_status(
            "COMPLETED",
            cost_per_hr=Decimal("0.80"),
        )
        assert result.actual_cost is None

    def test_non_terminal_has_no_cost(self) -> None:
        from pitwall.reconciler import map_runpod_status

        result = map_runpod_status(
            "IN_PROGRESS",
            cost_per_hr=Decimal("0.80"),
            worker_time_ms=5000,
        )
        assert result.terminal is False
        assert result.actual_cost is None

    def test_actual_cost_short_run(self) -> None:
        from pitwall.reconciler import map_runpod_status

        cost_per_hr = Decimal("0.44")
        worker_time_ms = 12_000
        result = map_runpod_status(
            "COMPLETED",
            cost_per_hr=cost_per_hr,
            worker_time_ms=worker_time_ms,
        )
        assert result.actual_cost is not None
        expected = (Decimal("0.44") / Decimal(3_600_000) * Decimal(12_000)).quantize(
            Decimal("0.000001")
        )
        assert result.actual_cost == expected


class TestRunPodJobStatusModel:
    def test_non_terminal_default_fields(self) -> None:
        from pitwall.reconciler import RunPodJobStatus

        status = RunPodJobStatus(terminal=False)
        assert status.terminal is False
        assert status.state is None
        assert status.actual_cost is None
        assert status.completed_at is None

    def test_terminal_with_all_fields(self) -> None:
        from pitwall.core.enums import WorkloadState
        from pitwall.reconciler import RunPodJobStatus

        ts = dt.datetime(2025, 6, 1, 12, 0, 0, tzinfo=dt.UTC)
        status = RunPodJobStatus(
            terminal=True,
            state=WorkloadState.COMPLETED,
            actual_cost=Decimal("0.123456"),
            completed_at=ts,
        )
        assert status.terminal is True
        assert status.state == WorkloadState.COMPLETED
        assert status.actual_cost == Decimal("0.123456")
        assert status.completed_at == ts


class TestComputeActualCost:
    def test_cost_from_per_hr_and_ms(self) -> None:
        from pitwall.reconciler import _compute_actual_cost

        cost = _compute_actual_cost(Decimal("3.60"), 1_000)
        assert cost is not None
        expected = (Decimal("3.60") / Decimal(3_600_000) * Decimal(1_000)).quantize(
            Decimal("0.000001")
        )
        assert cost == expected

    def test_none_when_cost_per_hr_missing(self) -> None:
        from pitwall.reconciler import _compute_actual_cost

        assert _compute_actual_cost(None, 1_000) is None

    def test_none_when_worker_time_missing(self) -> None:
        from pitwall.reconciler import _compute_actual_cost

        assert _compute_actual_cost(Decimal("1.00"), None) is None

    def test_none_when_worker_time_zero(self) -> None:
        from pitwall.reconciler import _compute_actual_cost

        assert _compute_actual_cost(Decimal("1.00"), 0) is None

    def test_none_when_both_none(self) -> None:
        from pitwall.reconciler import _compute_actual_cost

        assert _compute_actual_cost(None, None) is None


class TestApplyTerminalStateSql:
    def test_apply_terminal_state_module_contains_expected_sql(self) -> None:
        from pitwall.reconciler import _APPLY_TERMINAL_SQL

        assert "UPDATE pitwall.workloads" in _APPLY_TERMINAL_SQL
        assert "state = $1" in _APPLY_TERMINAL_SQL
        assert "cost_actual_usd = $2" in _APPLY_TERMINAL_SQL
        assert "completed_at = $3" in _APPLY_TERMINAL_SQL
        assert "WHERE id = $4" in _APPLY_TERMINAL_SQL

    def test_apply_terminal_state_is_exported(self) -> None:
        from pitwall.reconciler import __all__

        assert "apply_terminal_state" in __all__

    def test_map_runpod_status_is_exported(self) -> None:
        from pitwall.reconciler import __all__

        assert "map_runpod_status" in __all__

    def test_runpod_job_status_is_exported(self) -> None:
        from pitwall.reconciler import __all__

        assert "RunPodJobStatus" in __all__


class TestApplyTerminalStateIntegration:
    def test_apply_terminal_state_updates_workload_row(self) -> None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            pytest.skip("DATABASE_URL is required for the apply terminal state test")

        sql = _build_apply_terminal_state_sql()
        result = _run_sql(database_url, sql)
        assert result.returncode == 0, (
            f"apply terminal state test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _run_sql(database_url: str, sql: str) -> subprocess.CompletedProcess[str]:
    psql = _real_host_psql(database_url)
    if psql is not None:
        return subprocess.run(
            [psql, database_url, "-v", "ON_ERROR_STOP=1"],
            input=sql,
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    docker = shutil.which("docker")
    if docker is None or not _test_postgres_container_running(docker):
        pytest.skip("real psql or the test Postgres Docker container is required")

    parsed = urlparse(database_url)
    user = parsed.username or "pitwall"
    database = parsed.path.removeprefix("/") or "pitwall_test"
    return subprocess.run(
        [
            docker,
            "exec",
            "-i",
            _TEST_POSTGRES_CONTAINER,
            "psql",
            "-U",
            user,
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input=sql,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _real_host_psql(database_url: str) -> str | None:
    psql = shutil.which("psql")
    if psql is None:
        return None

    probe = subprocess.run(
        [
            psql,
            database_url,
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            "SELECT 'pitwall_psql_probe';",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if probe.returncode == 0 and "pitwall_psql_probe" in probe.stdout:
        return psql
    return None


def _test_postgres_container_running(docker: str) -> bool:
    result = subprocess.run(
        [
            docker,
            "inspect",
            "-f",
            "{{.State.Running}}",
            _TEST_POSTGRES_CONTAINER,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _build_apply_terminal_state_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_CAPABILITIES_SQL}
{_PROVIDERS_SQL}
{_WORKLOADS_SQL}
{_COST_COLUMNS_SQL}

INSERT INTO pitwall.capabilities (
  id, name, version, class, cost_mode, config
) VALUES (
  'cap_embed', 'Embedding', 'v1', 'inference', 'per_token', '{{}}'::jsonb
);

INSERT INTO pitwall.providers (
  id, capability_id, name, provider_type, config, priority
) VALUES (
  'prov_runpod_bge', 'cap_embed', 'RunPod BGE-M3',
  'serverless_queue', '{{"gpu_type_priority":["NVIDIA L4"]}}'::jsonb, 1
);

INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, runpod_job_id
) VALUES (
  'wl_reconcile_1', 'cap_embed', 'prov_runpod_bge', 'inference', 'running',
  now(), 'rp_job_001'
);

UPDATE pitwall.workloads
SET state = 'completed', cost_actual_usd = 0.043210, completed_at = now()
WHERE id = 'wl_reconcile_1';

DO $$
DECLARE
  v_state TEXT;
  v_cost NUMERIC;
  v_completed BOOLEAN;
BEGIN
  SELECT state, cost_actual_usd, completed_at IS NOT NULL
  INTO v_state, v_cost, v_completed
  FROM pitwall.workloads
  WHERE id = 'wl_reconcile_1';

  ASSERT v_state = 'completed',
    'expected state = completed, got ' || v_state;
  ASSERT v_cost = 0.043210,
    'expected cost_actual_usd = 0.043210, got ' || COALESCE(v_cost::text, 'NULL');
  ASSERT v_completed IS TRUE,
    'expected completed_at to be set';
END
$$;

ROLLBACK;
"""
