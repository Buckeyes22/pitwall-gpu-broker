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


def test_alert_events_migration_matches_spec_columns() -> None:
    sql = (_MIGRATION_DIR / "0012_alert_events.sql").read_text()

    assert "CREATE TABLE pitwall.alert_events" in sql
    assert "month          TEXT NOT NULL" in sql
    assert "threshold_pct  INTEGER NOT NULL" in sql
    assert "sent_at        TIMESTAMPTZ NOT NULL DEFAULT now()" in sql
    assert "PRIMARY KEY (month, threshold_pct)" in sql


def test_alert_events_insert_and_constraints() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the alert_events migration test")

    result = _run_sql(database_url)

    assert result.returncode == 0, (
        f"alert_events migration test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


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
_MIGRATION_SQL = (_MIGRATION_DIR / "0012_alert_events.sql").read_text()


def _build_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_SCHEMA_SQL}
{_MIGRATION_SQL}

-- 1. Insert alert events for different thresholds in same month
INSERT INTO pitwall.alert_events (month, threshold_pct, sent_at)
VALUES
  ('2026-01', 50, now()),
  ('2026-01', 75, now()),
  ('2026-01', 90, now());

-- 2. Verify composite PK enforces uniqueness: same (month, threshold_pct) must fail
DO $$
BEGIN
  INSERT INTO pitwall.alert_events (month, threshold_pct)
  VALUES ('2026-01', 50);
  RAISE EXCEPTION 'expected unique violation on composite PK';
EXCEPTION WHEN unique_violation THEN
  NULL;
END
$$;

-- 3. Same threshold_pct in different month is allowed (idempotency is per-month)
INSERT INTO pitwall.alert_events (month, threshold_pct)
VALUES ('2026-02', 50);

-- 4. NOT NULL constraints
DO $$
BEGIN
  INSERT INTO pitwall.alert_events (threshold_pct)
  VALUES (50);
  RAISE EXCEPTION 'expected NOT NULL violation on month';
EXCEPTION WHEN not_null_violation THEN
  NULL;
END
$$;

DO $$
BEGIN
  INSERT INTO pitwall.alert_events (month)
  VALUES ('2026-01');
  RAISE EXCEPTION 'expected NOT NULL violation on threshold_pct';
EXCEPTION WHEN not_null_violation THEN
  NULL;
END
$$;

-- 5. Verify all expected columns exist with correct types
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM information_schema.columns
  WHERE table_schema = 'pitwall'
    AND table_name = 'alert_events'
    AND (
      (column_name = 'month' AND udt_name = 'text')
      OR (column_name = 'threshold_pct' AND udt_name = 'int4')
      OR (column_name = 'sent_at' AND udt_name = 'timestamptz')
    );
  ASSERT v_count = 3, 'expected 3 columns with correct types, found ' || v_count;
END
$$;

-- 6. Verify composite primary key exists
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_constraint
  WHERE connamespace = 'pitwall'::regnamespace
    AND conrelid = 'pitwall.alert_events'::regclass
    AND contype = 'p';
  ASSERT v_count = 1, 'expected composite PK on alert_events';
END
$$;

-- 7. Verify sent_at default is now()
DO $$
DECLARE
  v_sent_at TIMESTAMPTZ;
BEGIN
  INSERT INTO pitwall.alert_events (month, threshold_pct)
  VALUES ('2026-03', 50)
  RETURNING sent_at INTO v_sent_at;

  ASSERT v_sent_at IS NOT NULL, 'expected sent_at to have default value';
  ASSERT ABS(EXTRACT(EPOCH FROM (now() - v_sent_at))) < 5,
    'expected sent_at default to be now()';
END
$$;

ROLLBACK;
"""
