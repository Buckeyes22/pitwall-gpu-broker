"""Full migration suite and repository-level integration tests.

Applies every migration in a disposable transaction (BEGIN / ROLLBACK) and
verifies:
  1. All migrations apply cleanly with no errors.
  2. Every expected index exists across all pitwall tables.
  3. JSONB config round-trip: insert JSONB into capabilities/providers config
     and read it back with full fidelity.
  4. Transactional audit writes: config_audit rows are visible inside the
     transaction and cleaned up on ROLLBACK.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

from pitwall.migrations import discover_migrations

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"
_TEST_POSTGRES_CONTAINER = "pitwall-test-postgres"

_ALL_MIGRATIONS = sorted(_MIGRATION_DIR.glob("*.sql"))
_ALL_MIGRATION_SQL = "\n".join(p.read_text() for p in _ALL_MIGRATIONS)


class TestAllMigrationsStatic:
    """Static analysis -- no database required."""

    def test_migration_files_exist(self) -> None:
        # Migrations only ever grow; guard against accidental deletion below the known
        # baseline without pinning an exact count that every new migration must bump.
        assert len(_ALL_MIGRATIONS) >= 13, (
            f"expected at least 13 migration files, found {len(_ALL_MIGRATIONS)}: "
            f"{[p.name for p in _ALL_MIGRATIONS]}"
        )

    def test_migrations_are_lexically_ordered(self) -> None:
        names = [p.name for p in _ALL_MIGRATIONS]
        assert names == sorted(names)

    def test_every_migration_creates_in_pitwall_schema(self) -> None:
        for path in _ALL_MIGRATIONS:
            sql = path.read_text()
            assert "pitwall." in sql, f"{path.name} does not reference pitwall schema"

    def test_capabilities_has_jsonb_config_column(self) -> None:
        sql = (_MIGRATION_DIR / "0001_capabilities.sql").read_text()
        assert "config                   JSONB NOT NULL" in sql

    def test_providers_has_jsonb_config_column(self) -> None:
        sql = (_MIGRATION_DIR / "0002_providers.sql").read_text()
        assert "config                   JSONB NOT NULL" in sql

    def test_config_audit_has_jsonb_value_columns(self) -> None:
        sql = (_MIGRATION_DIR / "0007_config_audit.sql").read_text()
        assert "old_value                JSONB" in sql
        assert "new_value                JSONB" in sql

    def test_discover_migrations_returns_all_files(self) -> None:
        records = discover_migrations(_MIGRATION_DIR)
        # Discovery must return exactly the .sql files on disk — stronger than a literal.
        assert len(records) == len(_ALL_MIGRATIONS)
        versions = [r.version for r in records]
        assert versions == sorted(versions)


class TestFullMigrationIntegration:
    """Integration tests -- require a running Postgres (DATABASE_URL or Docker)."""

    def test_all_migrations_apply_cleanly(self) -> None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            pytest.skip("DATABASE_URL is required for the full migration test")

        result = _run_sql(database_url, _build_all_migrations_sql())

        assert result.returncode == 0, (
            f"full migration apply failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_all_indexes_exist_after_migrations(self) -> None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            pytest.skip("DATABASE_URL is required for the index verification test")

        result = _run_sql(database_url, _build_index_verification_sql())

        assert result.returncode == 0, (
            f"index verification failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_jsonb_config_round_trip(self) -> None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            pytest.skip("DATABASE_URL is required for the JSONB round-trip test")

        result = _run_sql(database_url, _build_jsonb_round_trip_sql())

        assert result.returncode == 0, (
            "JSONB config round-trip test failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    def test_transactional_audit_writes(self) -> None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            pytest.skip("DATABASE_URL is required for the audit transaction test")

        result = _run_sql(database_url, _build_audit_transaction_sql())

        assert result.returncode == 0, (
            "transactional audit writes test failed\n"
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
            timeout=60,
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
        timeout=60,
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
        [docker, "inspect", "-f", "{{.State.Running}}", _TEST_POSTGRES_CONTAINER],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _build_all_migrations_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_ALL_MIGRATION_SQL}

-- 1. Verify all 10 pitwall tables were created.
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM information_schema.tables
  WHERE table_schema = 'pitwall'
    AND table_type = 'BASE TABLE';
  ASSERT v_count >= 10,
    'expected at least 10 pitwall tables, found ' || v_count;
END
$$;

-- 2. Spot-check: insert a capability and a provider to prove the schema is usable.
INSERT INTO pitwall.capabilities
  (id, name, version, class, cost_mode, config, created_at, updated_at)
VALUES
  ('cap-full-test', 'full-test-cap', '1.0.0', 'embedding', 'per_request',
   '{{"description": "full migration test", "input_schema": {{"type": "object"}},
     "output_schema": {{"type": "object"}}, "defaults": {{"execution_timeout_ms": 60000}},
     "hints_supported": ["latency_sensitive"]}}'::jsonb,
   now(), now());

INSERT INTO pitwall.providers
  (id, capability_id, name, provider_type, config, priority, updated_at)
VALUES
  ('prov-full-test', 'cap-full-test', 'full-test-prov', 'serverless_queue',
   '{{"gpu_type": "NVIDIA A100 80GB", "container_disk_gb": 50}}'::jsonb,
   1, now());

-- 3. Verify the provider FK references the capability.
DO $$
DECLARE
  v_cap_name TEXT;
BEGIN
  SELECT c.name INTO v_cap_name
  FROM pitwall.providers p
  JOIN pitwall.capabilities c ON c.id = p.capability_id
  WHERE p.id = 'prov-full-test';
  ASSERT v_cap_name = 'full-test-cap',
    'FK join failed, got cap name: ' || v_cap_name;
END
$$;

ROLLBACK;
"""


def _build_index_verification_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_ALL_MIGRATION_SQL}

-- 1. idx_providers_capability_priority
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'providers'
    AND indexname = 'idx_providers_capability_priority';
  ASSERT v_count = 1,
    'expected idx_providers_capability_priority, found ' || v_count;
END
$$;

-- 2. idx_workloads_idempotency
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'workloads'
    AND indexname = 'idx_workloads_idempotency';
  ASSERT v_count = 1,
    'expected idx_workloads_idempotency, found ' || v_count;
END
$$;

-- 3. idx_workloads_state_submitted
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'workloads'
    AND indexname = 'idx_workloads_state_submitted';
  ASSERT v_count = 1,
    'expected idx_workloads_state_submitted, found ' || v_count;
END
$$;

-- 4. idx_workloads_month_spend
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'workloads'
    AND indexname = 'idx_workloads_month_spend';
  ASSERT v_count = 1,
    'expected idx_workloads_month_spend, found ' || v_count;
END
$$;

-- 5. idx_leases_expires
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'leases'
    AND indexname = 'idx_leases_expires';
  ASSERT v_count = 1,
    'expected idx_leases_expires, found ' || v_count;
END
$$;

-- 6. idx_runpod_templates_image_sha
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'runpod_templates'
    AND indexname = 'idx_runpod_templates_image_sha';
  ASSERT v_count = 1,
    'expected idx_runpod_templates_image_sha, found ' || v_count;
END
$$;

-- 7. idx_kill_log_triggered
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'kill_log'
    AND indexname = 'idx_kill_log_triggered';
  ASSERT v_count = 1,
    'expected idx_kill_log_triggered, found ' || v_count;
END
$$;

-- 8. idx_audit_entity
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'config_audit'
    AND indexname = 'idx_audit_entity';
  ASSERT v_count = 1,
    'expected idx_audit_entity, found ' || v_count;
END
$$;

-- 9. idx_volumes_datacenter
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'volumes'
    AND indexname = 'idx_volumes_datacenter';
  ASSERT v_count = 1,
    'expected idx_volumes_datacenter, found ' || v_count;
END
$$;

-- 10. Verify all index definitions (at least 9 non-implicit indexes)
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND indexname NOT LIKE '%_pkey';
  ASSERT v_count >= 9,
    'expected at least 9 non-PK indexes in pitwall, found ' || v_count;
END
$$;

ROLLBACK;
"""


def _build_jsonb_round_trip_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_ALL_MIGRATION_SQL}

-- 1. Insert a capability with nested JSONB config and read it back.
INSERT INTO pitwall.capabilities
  (id, name, version, class, cost_mode, config, created_at, updated_at)
VALUES
  ('cap-jsonb-rt', 'jsonb-roundtrip-cap', '2.0.0', 'llm', 'per_token',
   '{{
     "description": "JSONB round-trip test capability",
     "input_schema": {{
       "type": "object",
       "properties": {{
         "prompt": {{"type": "string", "maxLength": 4096}},
         "temperature": {{"type": "number", "minimum": 0, "maximum": 2}}
       }},
       "required": ["prompt"]
     }},
     "output_schema": {{
       "type": "object",
       "properties": {{
         "completion": {{"type": "string"}},
         "tokens_used": {{"type": "integer"}}
       }}
     }},
     "defaults": {{
       "execution_timeout_ms": 120000,
       "ttl_ms": 600000,
       "result_delivery": "async"
     }},
     "hints_supported": ["latency_sensitive", "cost_sensitive", "region_preference"]
   }}'::jsonb,
   now(), now());

-- 2. Verify every nested key survived the round trip.
DO $$
DECLARE
  v_config JSONB;
BEGIN
  SELECT config INTO v_config
  FROM pitwall.capabilities
  WHERE id = 'cap-jsonb-rt';

  ASSERT v_config ->> 'description' = 'JSONB round-trip test capability',
    'config.description did not round-trip';

  ASSERT (v_config -> 'input_schema' -> 'properties' -> 'prompt' ->> 'type') = 'string',
    'nested input_schema.properties.prompt.type did not round-trip';

  ASSERT (v_config -> 'input_schema' -> 'properties' -> 'temperature' ->> 'maximum') = '2',
    'nested input_schema.properties.temperature.maximum did not round-trip';

  ASSERT (v_config -> 'output_schema' -> 'properties' -> 'tokens_used' ->> 'type') = 'integer',
    'nested output_schema.properties.tokens_used.type did not round-trip';

  ASSERT (v_config -> 'defaults' ->> 'execution_timeout_ms') = '120000',
    'defaults.execution_timeout_ms did not round-trip';

  ASSERT jsonb_array_length(v_config -> 'hints_supported') = 3,
    'hints_supported array length did not round-trip';
END
$$;

-- 3. Insert a provider with JSONB config and read it back.
INSERT INTO pitwall.capabilities
  (id, name, version, class, cost_mode, config, created_at, updated_at)
VALUES
  ('cap-prov-rt', 'provider-rt-cap', '1.0.0', 'embedding', 'per_request',
   '{{}}'::jsonb, now(), now());

INSERT INTO pitwall.providers
  (id, capability_id, name, provider_type, config, priority, updated_at)
VALUES
  ('prov-jsonb-rt', 'cap-prov-rt', 'jsonb-roundtrip-prov', 'serverless_lb',
   '{{
     "gpu_type_priority": ["NVIDIA A100 80GB", "NVIDIA H100 80GB HBM3"],
     "container_disk_gb": 100,
     "env_vars": {{
       "MODEL_NAME": "bge-m3-v2",
       "MAX_BATCH_SIZE": "32"
     }},
     "volume_mount_path": "/data/models"
   }}'::jsonb,
   5, now());

-- 4. Verify provider config round-trip.
DO $$
DECLARE
  v_config JSONB;
  v_gpu_types JSONB;
BEGIN
  SELECT config INTO v_config
  FROM pitwall.providers
  WHERE id = 'prov-jsonb-rt';

  ASSERT (v_config ->> 'container_disk_gb') = '100',
    'provider config container_disk_gb did not round-trip';

  ASSERT (v_config -> 'env_vars' ->> 'MODEL_NAME') = 'bge-m3-v2',
    'nested provider config env_vars.MODEL_NAME did not round-trip';

  ASSERT (v_config -> 'env_vars' ->> 'MAX_BATCH_SIZE') = '32',
    'nested provider config env_vars.MAX_BATCH_SIZE did not round-trip';

  v_gpu_types := v_config -> 'gpu_type_priority';
  ASSERT jsonb_array_length(v_gpu_types) = 2,
    'gpu_type_priority array length did not round-trip';
  ASSERT v_gpu_types ->> 0 = 'NVIDIA A100 80GB',
    'gpu_type_priority[0] did not round-trip';
  ASSERT v_gpu_types ->> 1 = 'NVIDIA H100 80GB HBM3',
    'gpu_type_priority[1] did not round-trip';
END
$$;

-- 5. Verify config_audit JSONB columns round-trip.
INSERT INTO pitwall.config_audit
  (actor, action, entity_type, entity_id, old_value, new_value, change_reason)
VALUES
  ('rest:admin', 'update', 'capability', 'cap-jsonb-rt',
   '{{"cost_mode": "per_request", "version": "1.0.0"}}'::jsonb,
   '{{"cost_mode": "per_token", "version": "2.0.0"}}'::jsonb,
   'version bump');

DO $$
DECLARE
  v_old JSONB;
  v_new JSONB;
  v_reason TEXT;
BEGIN
  SELECT old_value, new_value, change_reason
  INTO v_old, v_new, v_reason
  FROM pitwall.config_audit
  WHERE entity_id = 'cap-jsonb-rt' AND action = 'update';

  ASSERT v_old ->> 'cost_mode' = 'per_request',
    'old_value cost_mode did not round-trip';
  ASSERT v_old ->> 'version' = '1.0.0',
    'old_value version did not round-trip';
  ASSERT v_new ->> 'cost_mode' = 'per_token',
    'new_value cost_mode did not round-trip';
  ASSERT v_new ->> 'version' = '2.0.0',
    'new_value version did not round-trip';
  ASSERT v_reason = 'version bump',
    'change_reason did not round-trip';
END
$$;

ROLLBACK;
"""


def _build_audit_transaction_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_ALL_MIGRATION_SQL}

-- 1. Insert a capability so FK targets exist for providers.
INSERT INTO pitwall.capabilities
  (id, name, version, class, cost_mode, config, created_at, updated_at)
VALUES
  ('cap-audit-tx', 'audit-tx-cap', '1.0.0', 'llm', 'per_token',
   '{{}}'::jsonb, now(), now());

-- 2. Write an audit entry inside the current transaction.
INSERT INTO pitwall.config_audit
  (actor, action, entity_type, entity_id, new_value)
VALUES
  ('system', 'create', 'capability', 'cap-audit-tx',
   '{{"name": "audit-tx-cap", "version": "1.0.0"}}'::jsonb);

-- 3. Verify the audit row is visible inside this transaction.
DO $$
DECLARE
  v_count INTEGER;
  v_actor TEXT;
  v_new_value JSONB;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pitwall.config_audit
  WHERE entity_id = 'cap-audit-tx' AND action = 'create';
  ASSERT v_count = 1,
    'expected 1 audit row visible inside transaction, found ' || v_count;

  SELECT actor, new_value INTO v_actor, v_new_value
  FROM pitwall.config_audit
  WHERE entity_id = 'cap-audit-tx' AND action = 'create';
  ASSERT v_actor = 'system',
    'expected actor=system, got ' || v_actor;
  ASSERT v_new_value ->> 'name' = 'audit-tx-cap',
    'new_value name did not round-trip in transaction';
END
$$;

-- 4. Write a second audit entry in the same transaction.
INSERT INTO pitwall.config_audit
  (actor, action, entity_type, entity_id, old_value, new_value, change_reason)
VALUES
  ('rest:admin', 'disable', 'provider', 'prov-test-audit',
   '{{"enabled": true}}'::jsonb,
   '{{"enabled": false}}'::jsonb,
   'operator disabled for maintenance');

-- 5. Verify both audit rows are visible.
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pitwall.config_audit
  WHERE entity_id IN ('cap-audit-tx', 'prov-test-audit');
  ASSERT v_count = 2,
    'expected 2 audit rows visible in transaction, found ' || v_count;
END
$$;

-- 6. Verify auto-generated id and default created_at.
DO $$
DECLARE
  v_id BIGINT;
  v_created_at TIMESTAMPTZ;
BEGIN
  SELECT id, created_at INTO v_id, v_created_at
  FROM pitwall.config_audit
  WHERE entity_id = 'cap-audit-tx' AND action = 'create';
  ASSERT v_id IS NOT NULL, 'expected auto-generated audit id';
  ASSERT v_created_at IS NOT NULL, 'expected created_at default';
  ASSERT ABS(EXTRACT(EPOCH FROM (now() - v_created_at))) < 5,
    'expected created_at to default to now()';
END
$$;

-- 7. Verify the idx_audit_entity index can be used for lookups.
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pitwall.config_audit
  WHERE entity_type = 'capability'
    AND entity_id = 'cap-audit-tx';
  ASSERT v_count = 1,
    'idx_audit_entity lookup expected 1 row, found ' || v_count;
END
$$;

-- ROLLBACK proves the audit writes were transactional and disposable.
ROLLBACK;

-- 8. Verify the audit rows are gone after rollback (in a new transaction).
BEGIN;
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pitwall.config_audit
  WHERE entity_id IN ('cap-audit-tx', 'prov-test-audit');
  ASSERT v_count = 0,
    'audit rows should not exist after rollback, found ' || v_count;
END
$$;
ROLLBACK;
"""
