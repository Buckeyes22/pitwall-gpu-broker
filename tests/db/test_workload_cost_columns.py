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


def test_workload_cost_columns_migration_has_expected_sql() -> None:
    sql = (_MIGRATION_DIR / "0011_workload_cost_columns.sql").read_text()

    assert "cost_estimate_usd" in sql
    assert "cost_actual_usd" in sql
    assert "NUMERIC(12,6)" in sql
    assert "idx_workloads_month_spend" in sql
    assert "IF NOT EXISTS" in sql
    assert "workloads_cost_estimate_nonneg" in sql
    assert "workloads_cost_actual_nonneg" in sql


def test_workload_cost_columns_mtd_spend_and_constraints() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the workload cost columns migration test")

    result = _run_sql(database_url)

    assert result.returncode == 0, (
        "workload cost columns migration test failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
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


_CAPABILITIES_SQL = (_MIGRATION_DIR / "0001_capabilities.sql").read_text()
_PROVIDERS_SQL = (_MIGRATION_DIR / "0002_providers.sql").read_text()
_WORKLOADS_SQL = (_MIGRATION_DIR / "0003_workloads.sql").read_text()
_COST_COLUMNS_SQL = (_MIGRATION_DIR / "0011_workload_cost_columns.sql").read_text()


def _build_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_CAPABILITIES_SQL}
{_PROVIDERS_SQL}
{_WORKLOADS_SQL}
{_COST_COLUMNS_SQL}

-- Seed a capability + provider so workloads have valid FK targets
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

-- 1. Workloads with positive cost_estimate_usd are accepted
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, cost_estimate_usd
) VALUES (
  'wl_cost_001', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued',
  now(), 0.001250
);

-- 2. Workload with cost_actual_usd set on completion
UPDATE pitwall.workloads
SET state = 'completed', completed_at = now(),
    cost_actual_usd = 0.000890
WHERE id = 'wl_cost_001';

-- 3. MTD spend query (try_launch pattern, §9.2) returns the expected sum
DO $$
DECLARE
  v_mtd NUMERIC;
BEGIN
  SELECT COALESCE(SUM(cost_estimate_usd), 0) INTO v_mtd
  FROM pitwall.workloads
  WHERE submitted_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
    AND state IN ('queued','running','completed');

  ASSERT v_mtd IS NOT NULL, 'MTD spend query returned NULL';
END
$$;

-- 4. Negative cost_estimate_usd must be rejected by CHECK constraint
DO $$
BEGIN
  INSERT INTO pitwall.workloads (
    id, capability_id, provider_id, type, state,
    submitted_at, cost_estimate_usd
  ) VALUES (
    'wl_neg_est', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued',
    now(), -0.001
  );
  RAISE EXCEPTION 'expected negative cost_estimate_usd to be rejected';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- 5. Negative cost_actual_usd must be rejected by CHECK constraint
DO $$
BEGIN
  INSERT INTO pitwall.workloads (
    id, capability_id, provider_id, type, state,
    submitted_at, cost_actual_usd
  ) VALUES (
    'wl_neg_act', 'cap_embed', 'prov_runpod_bge', 'inference', 'completed',
    now(), -0.005
  );
  RAISE EXCEPTION 'expected negative cost_actual_usd to be rejected';
EXCEPTION WHEN check_violation THEN
  NULL;
END
$$;

-- 6. NULL cost values are allowed (not yet estimated / not yet actual)
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state, submitted_at
) VALUES (
  'wl_null_costs', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued', now()
);

-- 7. Zero cost values are allowed
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, cost_estimate_usd, cost_actual_usd
) VALUES (
  'wl_zero_costs', 'cap_embed', 'prov_runpod_bge', 'inference', 'completed',
  now(), 0.000000, 0.000000
);

-- 8. Verify the MTD spend index exists
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'workloads'
    AND indexname = 'idx_workloads_month_spend';
  ASSERT v_count = 1, 'expected idx_workloads_month_spend to exist';
END
$$;

-- 9. Verify CHECK constraints exist
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_constraint
  WHERE connamespace = 'pitwall'::regnamespace
    AND conrelid = 'pitwall.workloads'::regclass
    AND conname IN ('workloads_cost_estimate_nonneg', 'workloads_cost_actual_nonneg');
  ASSERT v_count = 2, 'expected 2 cost non-negative CHECK constraints, found ' || v_count;
END
$$;

ROLLBACK;
"""
