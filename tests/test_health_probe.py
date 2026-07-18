"""Tests for the Arq health probe job in the reconciler."""

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
_COOLDOWN_SQL = (_MIGRATION_DIR / "0013_provider_cooldown_state.sql").read_text()


def test_health_probe_providers_sql_is_exported() -> None:
    from pitwall.reconciler import _HEALTH_PROBE_PROVIDERS_SQL

    assert "serverless_lb" in _HEALTH_PROBE_PROVIDERS_SQL
    assert "runpod_endpoint_id IS NOT NULL" in _HEALTH_PROBE_PROVIDERS_SQL
    assert "enabled = true" in _HEALTH_PROBE_PROVIDERS_SQL


def test_update_provider_health_sql_is_exported() -> None:
    from pitwall.reconciler import _UPDATE_PROVIDER_HEALTH_SQL

    assert "UPDATE pitwall.providers" in _UPDATE_PROVIDER_HEALTH_SQL
    assert "health_status" in _UPDATE_PROVIDER_HEALTH_SQL
    assert "consecutive_failures" in _UPDATE_PROVIDER_HEALTH_SQL
    assert "cooldown_trips" in _UPDATE_PROVIDER_HEALTH_SQL
    assert "cooldown_until" in _UPDATE_PROVIDER_HEALTH_SQL


def test_fetch_providers_for_health_probe_is_exported() -> None:
    from pitwall.reconciler import __all__

    assert "fetch_providers_for_health_probe" in __all__


def test_update_provider_health_is_exported() -> None:
    from pitwall.reconciler import __all__

    assert "update_provider_health" in __all__


def test_health_probe_sql_selects_enabled_lb_providers() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the health probe SQL test")

    result = _run_sql(database_url, _build_health_probe_sql())

    assert result.returncode == 0, (
        f"health probe SQL test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
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


def _build_health_probe_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_CAPABILITIES_SQL}
{_PROVIDERS_SQL}
{_COOLDOWN_SQL}

-- Seed a capability so providers have valid FK targets
INSERT INTO pitwall.capabilities (
  id, name, version, class, cost_mode, config
) VALUES (
  'cap_embed', 'Embedding', 'v1', 'inference', 'per_token', '{{}}'::jsonb
);

-- 1. Enabled LB provider with endpoint_id should be returned
INSERT INTO pitwall.providers (
  id, capability_id, name, provider_type, config, priority,
  runpod_endpoint_id, enabled, health_status, consecutive_failures, cooldown_trips
) VALUES (
  'prov_lb_healthy', 'cap_embed', 'RunPod LB 1',
  'serverless_lb', '{{}}'::jsonb, 1,
  'endpoint_123', true, 'healthy', 0, 0
);

-- 2. Enabled LB provider without endpoint_id should be excluded
INSERT INTO pitwall.providers (
  id, capability_id, name, provider_type, config, priority,
  enabled, health_status, consecutive_failures, cooldown_trips
) VALUES (
  'prov_lb_no_endpoint', 'cap_embed', 'RunPod LB No Endpoint',
  'serverless_lb', '{{}}'::jsonb, 2,
  true, 'unknown', 0, 0
);

-- 3. Disabled LB provider should be excluded
INSERT INTO pitwall.providers (
  id, capability_id, name, provider_type, config, priority,
  runpod_endpoint_id, enabled, health_status, consecutive_failures, cooldown_trips
) VALUES (
  'prov_lb_disabled', 'cap_embed', 'RunPod LB Disabled',
  'serverless_lb', '{{}}'::jsonb, 3,
  'endpoint_456', false, 'unknown', 0, 0
);

-- 4. Enabled serverless_queue provider should be excluded (wrong type)
INSERT INTO pitwall.providers (
  id, capability_id, name, provider_type, config, priority,
  runpod_endpoint_id, enabled, health_status, consecutive_failures, cooldown_trips
) VALUES (
  'prov_queue', 'cap_embed', 'RunPod Queue',
  'serverless_queue', '{{}}'::jsonb, 4,
  'endpoint_789', true, 'unknown', 0, 0
);

-- 5. Verify exactly 1 provider is returned (the enabled LB provider with endpoint_id)
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pitwall.providers
  WHERE enabled = true
    AND runpod_endpoint_id IS NOT NULL
    AND provider_type = 'serverless_lb';

  ASSERT v_count = 1,
    'expected exactly 1 health probe target, found ' || v_count;
END
$$;

-- 6. Verify the correct provider is returned
DO $$
DECLARE
  v_id TEXT;
BEGIN
  SELECT id INTO v_id
  FROM pitwall.providers
  WHERE enabled = true
    AND runpod_endpoint_id IS NOT NULL
    AND provider_type = 'serverless_lb';

  ASSERT v_id = 'prov_lb_healthy',
    'expected prov_lb_healthy, got ' || COALESCE(v_id, 'NULL');
END
$$;

-- 7. Verify the excluded providers are not returned
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pitwall.providers
  WHERE enabled = true
    AND runpod_endpoint_id IS NOT NULL
    AND provider_type = 'serverless_lb'
    AND id IN ('prov_lb_no_endpoint', 'prov_lb_disabled', 'prov_queue');

  ASSERT v_count = 0,
    'expected 0 excluded rows, found ' || v_count;
END
$$;

-- 8. Test update provider health SQL
UPDATE pitwall.providers
SET
    health_status = 'unhealthy',
    consecutive_failures = 3,
    cooldown_trips = 1,
    cooldown_until = now() + interval '5 minutes',
    updated_at = now()
WHERE id = 'prov_lb_healthy';

DO $$
DECLARE
  v_health TEXT;
  v_failures INTEGER;
  v_trips INTEGER;
BEGIN
  SELECT health_status, consecutive_failures, cooldown_trips INTO v_health, v_failures, v_trips
  FROM pitwall.providers
  WHERE id = 'prov_lb_healthy';

  ASSERT v_health = 'unhealthy',
    'expected health_status = unhealthy, got ' || COALESCE(v_health, 'NULL');
  ASSERT v_failures = 3,
    'expected consecutive_failures = 3, got ' || COALESCE(v_failures::text, 'NULL');
  ASSERT v_trips = 1,
    'expected cooldown_trips = 1, got ' || COALESCE(v_trips::text, 'NULL');
END
$$;

ROLLBACK;
"""
