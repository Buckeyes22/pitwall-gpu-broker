"""Aggregate SQL — join workloads to capabilities/providers,
group by UTC day/class/type, upsert into cost_daily.

Tests for:
  - _AGGREGATE_DAILY_SQL constant contains the expected join and GROUP BY
  - aggregate_daily_cost is exported
  - Integration: seed workloads, run aggregate, verify cost_daily rows
"""

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
_COST_DAILY_SQL = (_MIGRATION_DIR / "0009_cost_daily.sql").read_text()


def test_aggregate_daily_sql_module_contains_expected_sql() -> None:
    from pitwall.reconciler import _AGGREGATE_DAILY_SQL

    assert "INSERT INTO pitwall.cost_daily" in _AGGREGATE_DAILY_SQL
    assert "JOIN pitwall.capabilities" in _AGGREGATE_DAILY_SQL
    assert "JOIN pitwall.providers" in _AGGREGATE_DAILY_SQL
    assert "DATE(w.submitted_at AT TIME ZONE 'UTC')" in _AGGREGATE_DAILY_SQL
    assert "c.class" in _AGGREGATE_DAILY_SQL
    assert "p.provider_type" in _AGGREGATE_DAILY_SQL
    assert "GROUP BY" in _AGGREGATE_DAILY_SQL
    assert "COUNT(*)" in _AGGREGATE_DAILY_SQL
    assert "SUM(w.cost_actual_usd)" in _AGGREGATE_DAILY_SQL
    assert "ON CONFLICT" in _AGGREGATE_DAILY_SQL


def test_aggregate_daily_sql_filters_terminal_states() -> None:
    from pitwall.reconciler import _AGGREGATE_DAILY_SQL

    assert "'completed'" in _AGGREGATE_DAILY_SQL
    assert "'failed'" in _AGGREGATE_DAILY_SQL
    assert "'cancelled'" in _AGGREGATE_DAILY_SQL
    assert "'timed_out'" in _AGGREGATE_DAILY_SQL


def test_aggregate_daily_cost_is_exported() -> None:
    from pitwall.reconciler import __all__

    assert "aggregate_daily_cost" in __all__


def test_aggregate_daily_sql_joins_workloads_to_caps_and_providers() -> None:
    from pitwall.reconciler import _AGGREGATE_DAILY_SQL

    assert "c.id = w.capability_id" in _AGGREGATE_DAILY_SQL
    assert "p.id = w.provider_id" in _AGGREGATE_DAILY_SQL


def test_aggregate_daily_sql_upserts_on_conflict() -> None:
    from pitwall.reconciler import _AGGREGATE_DAILY_SQL

    assert "ON CONFLICT (day, capability_class, provider_type)" in _AGGREGATE_DAILY_SQL
    assert "DO UPDATE SET" in _AGGREGATE_DAILY_SQL
    assert "workload_count = EXCLUDED.workload_count" in _AGGREGATE_DAILY_SQL
    assert "cost_usd" in _AGGREGATE_DAILY_SQL
    assert "= EXCLUDED.cost_usd" in _AGGREGATE_DAILY_SQL


def test_aggregate_daily_integration() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the aggregate daily integration test")

    result = _run_sql(database_url, _build_integration_sql())

    assert result.returncode == 0, (
        "aggregate daily integration test failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
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


def _build_integration_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_CAPABILITIES_SQL}
{_PROVIDERS_SQL}
{_WORKLOADS_SQL}
{_COST_COLUMNS_SQL}
{_COST_DAILY_SQL}

-- Seed: capability (class=embedding), provider (type=serverless_queue)
INSERT INTO pitwall.capabilities (
  id, name, version, class, cost_mode, config
) VALUES (
  'cap_embed', 'Embedding', 'v1', 'embedding', 'per_request', '{{}}'::jsonb
);

INSERT INTO pitwall.providers (
  id, capability_id, name, provider_type, config, priority
) VALUES (
  'prov_queue', 'cap_embed', 'RunPod BGE-M3',
  'serverless_queue', '{{"gpu_type_priority":["NVIDIA L4"]}}'::jsonb, 1
);

-- Seed: capability (class=llm), provider (type=serverless_lb)
INSERT INTO pitwall.capabilities (
  id, name, version, class, cost_mode, config
) VALUES (
  'cap_llm', 'LLM', 'v1', 'llm', 'per_token', '{{}}'::jsonb
);

INSERT INTO pitwall.providers (
  id, capability_id, name, provider_type, config, priority
) VALUES (
  'prov_lb', 'cap_llm', 'RunPod LLM-LB',
  'serverless_lb', '{{"gpu_type_priority":["NVIDIA L4"]}}'::jsonb, 1
);

-- Completed workload on 2026-01-15 (embedding/serverless_queue) with cost
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, cost_actual_usd
) VALUES (
  'wl_agg_1', 'cap_embed', 'prov_queue', 'embedding', 'completed',
  '2026-01-15T10:00:00+00:00', 0.500000
);

-- Completed workload on 2026-01-15 (embedding/serverless_queue) with cost
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, cost_actual_usd
) VALUES (
  'wl_agg_2', 'cap_embed', 'prov_queue', 'embedding', 'completed',
  '2026-01-15T14:00:00+00:00', 0.250000
);

-- Completed workload on 2026-01-15 (llm/serverless_lb) with cost
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, cost_actual_usd
) VALUES (
  'wl_agg_3', 'cap_llm', 'prov_lb', 'llm', 'completed',
  '2026-01-15T18:00:00+00:00', 1.200000
);

-- Failed workload on 2026-01-16 (embedding/serverless_queue) with cost
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, cost_actual_usd
) VALUES (
  'wl_agg_4', 'cap_embed', 'prov_queue', 'embedding', 'failed',
  '2026-01-16T08:00:00+00:00', 0.100000
);

-- Running workload — should be EXCLUDED from aggregate
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state,
  submitted_at, cost_actual_usd
) VALUES (
  'wl_agg_skip', 'cap_embed', 'prov_queue', 'embedding', 'running',
  '2026-01-15T12:00:00+00:00', 0.300000
);

-- Workload with NULL cost_actual_usd — should contribute to count but 0 cost
INSERT INTO pitwall.workloads (
  id, capability_id, provider_id, type, state, submitted_at
) VALUES (
  'wl_agg_null', 'cap_embed', 'prov_queue', 'embedding', 'completed',
  '2026-01-17T08:00:00+00:00'
);

-- Run the aggregate SQL
INSERT INTO pitwall.cost_daily
    (day, capability_class, provider_type, workload_count, cost_usd)
SELECT
    DATE(w.submitted_at AT TIME ZONE 'UTC') AS day,
    c.class                                   AS capability_class,
    p.provider_type                            AS provider_type,
    COUNT(*)                                   AS workload_count,
    COALESCE(SUM(w.cost_actual_usd), 0)        AS cost_usd
FROM pitwall.workloads w
JOIN pitwall.capabilities c ON c.id = w.capability_id
JOIN pitwall.providers    p ON p.id = w.provider_id
WHERE w.state IN ('completed', 'failed', 'cancelled', 'timed_out')
GROUP BY day, c.class, p.provider_type
ON CONFLICT (day, capability_class, provider_type)
DO UPDATE SET
    workload_count = EXCLUDED.workload_count,
    cost_usd       = EXCLUDED.cost_usd;

-- Verify: 2026-01-15 / embedding / serverless_queue → 2 workloads, cost 0.75
DO $$
DECLARE
  v_count INTEGER;
  v_cost  NUMERIC;
BEGIN
  SELECT workload_count, cost_usd INTO v_count, v_cost
  FROM pitwall.cost_daily
  WHERE day = '2026-01-15'
    AND capability_class = 'embedding'
    AND provider_type = 'serverless_queue';

  ASSERT v_count = 2,
    'expected 2 embedding/serverless_queue workloads on 2026-01-15, got ' || v_count;
  ASSERT v_cost = 0.750000,
    'expected cost 0.750000 for embedding/serverless_queue on 2026-01-15, got ' || COALESCE(v_cost::text, 'NULL');
END
$$;

-- Verify: 2026-01-15 / llm / serverless_lb → 1 workload, cost 1.20
DO $$
DECLARE
  v_count INTEGER;
  v_cost  NUMERIC;
BEGIN
  SELECT workload_count, cost_usd INTO v_count, v_cost
  FROM pitwall.cost_daily
  WHERE day = '2026-01-15'
    AND capability_class = 'llm'
    AND provider_type = 'serverless_lb';

  ASSERT v_count = 1,
    'expected 1 llm/serverless_lb workload on 2026-01-15, got ' || v_count;
  ASSERT v_cost = 1.200000,
    'expected cost 1.200000 for llm/serverless_lb on 2026-01-15, got ' || COALESCE(v_cost::text, 'NULL');
END
$$;

-- Verify: 2026-01-16 / embedding / serverless_queue → 1 workload (failed), cost 0.10
DO $$
DECLARE
  v_count INTEGER;
  v_cost  NUMERIC;
BEGIN
  SELECT workload_count, cost_usd INTO v_count, v_cost
  FROM pitwall.cost_daily
  WHERE day = '2026-01-16'
    AND capability_class = 'embedding'
    AND provider_type = 'serverless_queue';

  ASSERT v_count = 1,
    'expected 1 embedding/serverless_queue workload on 2026-01-16, got ' || v_count;
  ASSERT v_cost = 0.100000,
    'expected cost 0.100000 for embedding/serverless_queue on 2026-01-16, got ' || COALESCE(v_cost::text, 'NULL');
END
$$;

-- Verify: 2026-01-17 / embedding / serverless_queue → 1 workload, cost 0.00 (NULL cost)
DO $$
DECLARE
  v_count INTEGER;
  v_cost  NUMERIC;
BEGIN
  SELECT workload_count, cost_usd INTO v_count, v_cost
  FROM pitwall.cost_daily
  WHERE day = '2026-01-17'
    AND capability_class = 'embedding'
    AND provider_type = 'serverless_queue';

  ASSERT v_count = 1,
    'expected 1 embedding/serverless_queue workload on 2026-01-17, got ' || v_count;
  ASSERT v_cost = 0.000000,
    'expected cost 0.000000 for NULL cost_actual_usd, got ' || COALESCE(v_cost::text, 'NULL');
END
$$;

-- Verify: running workload was NOT aggregated (no row for it on any day besides the 3 we have)
DO $$
DECLARE
  v_total INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_total FROM pitwall.cost_daily;
  ASSERT v_total = 4,
    'expected exactly 4 cost_daily rows (3 days for embedding/queue + 1 day for llm/lb), got ' || v_total;
END
$$;

ROLLBACK;
"""
