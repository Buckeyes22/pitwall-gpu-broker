from __future__ import annotations

import os
import shutil
import subprocess
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


def test_reconcile_query_module_contains_expected_sql() -> None:
    from pitwall.reconciler import _RECONCILE_QUERY

    assert "state IN" in _RECONCILE_QUERY
    assert "'queued'" in _RECONCILE_QUERY
    assert "'running'" in _RECONCILE_QUERY
    assert "runpod_job_id IS NOT NULL" in _RECONCILE_QUERY


def test_fetch_active_workloads_is_exported() -> None:
    from pitwall.reconciler import __all__

    assert "fetch_active_workloads" in __all__


def test_reconcile_query_selects_active_with_runpod_job_id() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the reconcile query test")

    result = _run_sql(database_url, _build_reconcile_query_sql())

    assert result.returncode == 0, (
        f"reconcile query test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
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


def _build_reconcile_query_sql() -> str:
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

-- 1. Active workload with runpod_job_id should be returned
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, runpod_job_id
) VALUES (
  'wl_active_1', 'cap_embed', 'prov_runpod_bge', 'inference', 'running',
  now(), 'rp_job_001'
);

-- 2. Queued workload with runpod_job_id should be returned
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, runpod_job_id
) VALUES (
  'wl_active_2', 'cap_embed', 'prov_runpod_bge', 'inference', 'queued',
  now(), 'rp_job_002'
);

-- 3. Running workload WITHOUT runpod_job_id should be excluded
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state, submitted_at
) VALUES (
  'wl_no_job', 'cap_embed', 'prov_runpod_bge', 'inference', 'running', now()
);

-- 4. Completed workload with runpod_job_id should be excluded
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, runpod_job_id
) VALUES (
  'wl_completed', 'cap_embed', 'prov_runpod_bge', 'inference', 'completed',
  now(), 'rp_job_003'
);

-- 5. Failed workload with runpod_job_id should be excluded
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, runpod_job_id
) VALUES (
  'wl_failed', 'cap_embed', 'prov_runpod_bge', 'inference', 'failed',
  now(), 'rp_job_004'
);

-- 6. Verify the reconcile query returns exactly the 2 active rows with job ids
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pitwall.workloads
  WHERE state IN ('queued', 'running')
    AND runpod_job_id IS NOT NULL;

  ASSERT v_count = 2,
    'expected exactly 2 active workloads with runpod_job_id, found ' || v_count;
END
$$;

-- 7. Verify only the correct ids are returned
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pitwall.workloads
  WHERE state IN ('queued', 'running')
    AND runpod_job_id IS NOT NULL
    AND id IN ('wl_active_1', 'wl_active_2');

  ASSERT v_count = 2,
    'expected wl_active_1 and wl_active_2, missing ' || v_count;
END
$$;

-- 8. Verify the excluded rows are not returned
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pitwall.workloads
  WHERE state IN ('queued', 'running')
    AND runpod_job_id IS NOT NULL
    AND id IN ('wl_no_job', 'wl_completed', 'wl_failed');

  ASSERT v_count = 0,
    'expected 0 excluded rows, found ' || v_count;
END
$$;

-- 9. Verify the query returns id and runpod_job_id columns
DO $$
DECLARE
  v_id TEXT;
  v_job_id TEXT;
BEGIN
  SELECT id, runpod_job_id INTO v_id, v_job_id
  FROM pitwall.workloads
  WHERE state IN ('queued', 'running')
    AND runpod_job_id IS NOT NULL
    AND id = 'wl_active_1';

  ASSERT v_id = 'wl_active_1',
    'expected id = wl_active_1, got ' || v_id;
  ASSERT v_job_id = 'rp_job_001',
    'expected runpod_job_id = rp_job_001, got ' || COALESCE(v_job_id, 'NULL');
END
$$;

-- 10. Cancelled and timed_out workloads with runpod_job_id should be excluded
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, runpod_job_id
) VALUES
  ('wl_cancelled', 'cap_embed', 'prov_runpod_bge', 'inference', 'cancelled',
   now(), 'rp_job_005'),
  ('wl_timed_out', 'cap_embed', 'prov_runpod_bge', 'inference', 'timed_out',
   now(), 'rp_job_006');

DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pitwall.workloads
  WHERE state IN ('queued', 'running')
    AND runpod_job_id IS NOT NULL;

  ASSERT v_count = 2,
    'expected still exactly 2 after adding terminal-state rows, found ' || v_count;
END
$$;

ROLLBACK;
"""
