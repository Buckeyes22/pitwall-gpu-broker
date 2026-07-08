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


def test_cost_daily_migration_matches_spec_columns() -> None:
    sql = (_MIGRATION_DIR / "0009_cost_daily.sql").read_text()

    assert "CREATE TABLE pitwall.cost_daily" in sql
    assert "day                      DATE NOT NULL" in sql
    assert "capability_class         TEXT NOT NULL" in sql
    assert "provider_type            TEXT NOT NULL" in sql
    assert "workload_count           INTEGER NOT NULL" in sql
    assert "cost_usd                 NUMERIC(12,6) NOT NULL" in sql
    assert "PRIMARY KEY (day, capability_class, provider_type)" in sql


def test_cost_daily_insert_and_constraints() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the cost_daily migration test")

    result = _run_sql(database_url)

    assert result.returncode == 0, (
        f"cost_daily migration test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
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
_MIGRATION_SQL = (_MIGRATION_DIR / "0009_cost_daily.sql").read_text()


def _build_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_SCHEMA_SQL}
{_MIGRATION_SQL}

-- 1. Insert a valid rollup row
INSERT INTO pitwall.cost_daily
  (day, capability_class, provider_type, workload_count, cost_usd)
VALUES
  ('2026-01-15', 'inference', 'serverless_queue', 142, 1.234567);

-- 2. Verify composite PK enforces uniqueness: same (day, class, type) must fail
DO $$
BEGIN
  INSERT INTO pitwall.cost_daily
    (day, capability_class, provider_type, workload_count, cost_usd)
  VALUES
    ('2026-01-15', 'inference', 'serverless_queue', 99, 0.500000);
  RAISE EXCEPTION 'expected unique violation on composite PK';
EXCEPTION WHEN unique_violation THEN
  NULL;
END
$$;

-- 3. Same day, different capability_class or provider_type is allowed
INSERT INTO pitwall.cost_daily
  (day, capability_class, provider_type, workload_count, cost_usd)
VALUES
  ('2026-01-15', 'embedding', 'serverless_queue', 50, 0.100000),
  ('2026-01-15', 'inference', 'serverless_lb', 30, 0.250000);

-- 4. NOT NULL constraints on all columns
DO $$
BEGIN
  INSERT INTO pitwall.cost_daily (capability_class, provider_type, workload_count, cost_usd)
  VALUES ('inference', 'serverless_queue', 1, 0.001000);
  RAISE EXCEPTION 'expected NOT NULL violation on day';
EXCEPTION WHEN not_null_violation THEN
  NULL;
END
$$;

DO $$
BEGIN
  INSERT INTO pitwall.cost_daily (day, provider_type, workload_count, cost_usd)
  VALUES ('2026-01-16', 'serverless_queue', 1, 0.001000);
  RAISE EXCEPTION 'expected NOT NULL violation on capability_class';
EXCEPTION WHEN not_null_violation THEN
  NULL;
END
$$;

DO $$
BEGIN
  INSERT INTO pitwall.cost_daily (day, capability_class, workload_count, cost_usd)
  VALUES ('2026-01-16', 'inference', 1, 0.001000);
  RAISE EXCEPTION 'expected NOT NULL violation on provider_type';
EXCEPTION WHEN not_null_violation THEN
  NULL;
END
$$;

-- 5. NUMERIC(12,6) precision round-trip
DO $$
DECLARE
  v_cost NUMERIC;
BEGIN
  INSERT INTO pitwall.cost_daily
    (day, capability_class, provider_type, workload_count, cost_usd)
  VALUES
    ('2026-02-01', 'vision', 'public_endpoint', 1, 123456.789012);

  SELECT cost_usd INTO v_cost
  FROM pitwall.cost_daily
  WHERE day = '2026-02-01' AND capability_class = 'vision';

  ASSERT v_cost = 123456.789012, 'cost_usd did not round-trip, got ' || v_cost;
END
$$;

-- 6. Verify all expected columns exist with correct types
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM information_schema.columns
  WHERE table_schema = 'pitwall'
    AND table_name = 'cost_daily'
    AND (
      (column_name = 'day' AND udt_name = 'date')
      OR (column_name = 'capability_class' AND udt_name = 'text')
      OR (column_name = 'provider_type' AND udt_name = 'text')
      OR (column_name = 'workload_count' AND udt_name = 'int4')
      OR (column_name = 'cost_usd' AND udt_name = 'numeric')
    );
  ASSERT v_count = 5, 'expected 5 columns with correct types, found ' || v_count;
END
$$;

-- 7. Verify composite primary key exists
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_constraint
  WHERE connamespace = 'pitwall'::regnamespace
    AND conrelid = 'pitwall.cost_daily'::regclass
    AND contype = 'p';
  ASSERT v_count = 1, 'expected composite PK on cost_daily';
END
$$;

ROLLBACK;
"""
