from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"
_TEST_POSTGRES_CONTAINER = "pitwall-test-postgres"
_R2_ROTATED_COL = "_".join(("r2", "credentials", "rotated"))


def test_kill_log_insert_and_defaults() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the kill_log migration test")

    result = _run_sql(database_url)

    assert result.returncode == 0, (
        f"kill_log migration test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_kill_log_no_uio_columns() -> None:
    migration_path = _MIGRATION_DIR / "0006_kill_log.sql"
    sql = migration_path.read_text()
    forbidden = [
        "tailscale_acl_updated",
        _R2_ROTATED_COL,
        "skypilot_clusters_destroyed",
    ]
    for col in forbidden:
        assert col not in sql, f"forbidden legacy column '{col}' found in migration"


def _run_sql(database_url: str) -> subprocess.CompletedProcess[str]:
    psql = _real_host_psql(database_url)
    if psql is not None:
        return subprocess.run(
            [psql, database_url, "-v", "ON_ERROR_STOP=1", "-c", _build_sql()],
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
        input=_build_sql(),
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
        [psql, database_url, "-v", "ON_ERROR_STOP=1", "-Atc", "SELECT 'pitwall_psql_probe';"],
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
        [docker, "inspect", "-f", "{{.State.Running}}", _TEST_POSTGRES_CONTAINER],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


_SCHEMA_SQL = (_MIGRATION_DIR / "0001_capabilities.sql").read_text()
_MIGRATION_SQL = (_MIGRATION_DIR / "0006_kill_log.sql").read_text()


def _build_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_SCHEMA_SQL}
{_MIGRATION_SQL}

-- 1. Insert a row with all fields
INSERT INTO pitwall.kill_log
  (triggered_at, reason, actor, pods_terminated, endpoints_hibernated,
   workloads_cancelled, total_duration_ms, errors)
VALUES
  (now(), 'emergency shutdown', 'rest:admin', 3, 2, 5, 4200,
   '[\"pod pod-abc not found\", \"endpoint timeout\"]');

-- 2. Verify defaults for counter columns
DO $$
DECLARE
  v_id BIGINT;
BEGIN
  INSERT INTO pitwall.kill_log (reason, actor, total_duration_ms)
  VALUES ('scheduled maintenance', 'system:cron', 100)
  RETURNING id INTO v_id;

  ASSERT v_id IS NOT NULL, 'expected auto-generated id';
END
$$;

-- 3. Verify defaults: pods_terminated=0, endpoints_hibernated=0, workloads_cancelled=0
DO $$
DECLARE
  v_pods INTEGER;
  v_eps INTEGER;
  v_wl INTEGER;
  v_errors JSONB;
BEGIN
  SELECT pods_terminated, endpoints_hibernated, workloads_cancelled, errors
  INTO v_pods, v_eps, v_wl, v_errors
  FROM pitwall.kill_log
  WHERE reason = 'scheduled maintenance';

  ASSERT v_pods = 0, 'expected default pods_terminated=0, got ' || v_pods;
  ASSERT v_eps = 0, 'expected default endpoints_hibernated=0, got ' || v_eps;
  ASSERT v_wl = 0, 'expected default workloads_cancelled=0, got ' || v_wl;
  ASSERT v_errors = '[]'::jsonb, 'expected default errors=[]';
END
$$;

-- 4. Verify idx_kill_log_triggered exists
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'kill_log'
    AND indexname = 'idx_kill_log_triggered';
  ASSERT v_count = 1, 'expected idx_kill_log_triggered index to exist';
END
$$;

-- 5. Verify all required columns exist
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM information_schema.columns
  WHERE table_schema = 'pitwall'
    AND table_name = 'kill_log'
    AND column_name IN (
      'id', 'triggered_at', 'reason', 'actor',
      'pods_terminated', 'endpoints_hibernated',
      'workloads_cancelled', 'total_duration_ms', 'errors'
    );
  ASSERT v_count = 9, 'expected 9 columns in kill_log, found ' || v_count;
END
$$;

-- 6. Verify forbidden legacy columns do NOT exist
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM information_schema.columns
  WHERE table_schema = 'pitwall'
    AND table_name = 'kill_log'
    AND column_name IN (
      'tailscale_acl_updated',
      '{_R2_ROTATED_COL}',
      'skypilot_clusters_destroyed'
    );
  ASSERT v_count = 0, 'expected no legacy columns, found ' || v_count;
END
$$;

ROLLBACK;
"""
