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


def test_config_audit_migration_matches_spec_columns() -> None:
    sql = (_MIGRATION_DIR / "0007_config_audit.sql").read_text()

    assert "CREATE TABLE pitwall.config_audit" in sql
    assert "actor                    TEXT NOT NULL" in sql
    assert "action                   TEXT NOT NULL" in sql
    assert "entity_type              TEXT NOT NULL" in sql
    assert "entity_id                TEXT NOT NULL" in sql
    assert "old_value                JSONB" in sql
    assert "new_value                JSONB" in sql
    assert "change_reason            TEXT" in sql
    assert "created_at               TIMESTAMPTZ DEFAULT now()" in sql
    assert "idx_audit_entity" in sql
    assert "ON pitwall.config_audit(entity_type, entity_id, created_at DESC)" in sql


def test_config_audit_insert_requirements_and_index() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the config_audit migration test")

    result = _run_sql(database_url)

    assert result.returncode == 0, (
        f"config_audit migration test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
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


_SCHEMA_SQL = (_MIGRATION_DIR / "0001_capabilities.sql").read_text()
_MIGRATION_SQL = (_MIGRATION_DIR / "0007_config_audit.sql").read_text()


def _build_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_SCHEMA_SQL}
{_MIGRATION_SQL}

-- 1. The ticket's minimal acceptance insert must work.
INSERT INTO pitwall.config_audit(actor, action, entity_type, entity_id, new_value)
VALUES ('system', 'create', 'capability', 'cap_test', '{{}}'::jsonb);

-- 2. §12 actor examples remain plain compatible strings.
INSERT INTO pitwall.config_audit
  (actor, action, entity_type, entity_id, old_value, new_value, change_reason)
VALUES
  ('rest:admin', 'disable', 'provider', 'provider_a',
   '{{"enabled": true}}'::jsonb, '{{"enabled": false}}'::jsonb, 'operator action'),
  ('mcp:session-id', 'update', 'template', 'template_a',
   '{{"image_sha": "sha256:old"}}'::jsonb, '{{"image_sha": "sha256:new"}}'::jsonb,
   'session update');

-- 3. old_value remains nullable for create events and created_at defaults.
DO $$
DECLARE
  v_old JSONB;
  v_created_at TIMESTAMPTZ;
BEGIN
  SELECT old_value, created_at INTO v_old, v_created_at
  FROM pitwall.config_audit
  WHERE entity_id = 'cap_test';

  ASSERT v_old IS NULL, 'expected old_value to be nullable';
  ASSERT v_created_at IS NOT NULL, 'expected created_at default';
END
$$;

-- 4. old_value and new_value are JSONB columns.
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM information_schema.columns
  WHERE table_schema = 'pitwall'
    AND table_name = 'config_audit'
    AND column_name IN ('old_value', 'new_value')
    AND udt_name = 'jsonb';

  ASSERT v_count = 2, 'expected JSONB old_value and new_value columns';
END
$$;

-- 5. actor, action, entity_type, and entity_id are required.
DO $$
BEGIN
  INSERT INTO pitwall.config_audit(action, entity_type, entity_id)
  VALUES ('create', 'capability', 'missing_actor');
  RAISE EXCEPTION 'expected actor NOT NULL violation';
EXCEPTION WHEN not_null_violation THEN
  NULL;
END
$$;

DO $$
BEGIN
  INSERT INTO pitwall.config_audit(actor, entity_type, entity_id)
  VALUES ('system', 'capability', 'missing_action');
  RAISE EXCEPTION 'expected action NOT NULL violation';
EXCEPTION WHEN not_null_violation THEN
  NULL;
END
$$;

DO $$
BEGIN
  INSERT INTO pitwall.config_audit(actor, action, entity_id)
  VALUES ('system', 'create', 'missing_entity_type');
  RAISE EXCEPTION 'expected entity_type NOT NULL violation';
EXCEPTION WHEN not_null_violation THEN
  NULL;
END
$$;

DO $$
BEGIN
  INSERT INTO pitwall.config_audit(actor, action, entity_type)
  VALUES ('system', 'create', 'capability');
  RAISE EXCEPTION 'expected entity_id NOT NULL violation';
EXCEPTION WHEN not_null_violation THEN
  NULL;
END
$$;

-- 6. The entity lookup index exists with created_at descending.
DO $$
DECLARE
  v_indexdef TEXT;
BEGIN
  SELECT indexdef INTO v_indexdef
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'config_audit'
    AND indexname = 'idx_audit_entity';

  ASSERT v_indexdef IS NOT NULL, 'expected idx_audit_entity index to exist';
  ASSERT v_indexdef LIKE '%(entity_type, entity_id, created_at DESC)%',
    'expected idx_audit_entity on (entity_type, entity_id, created_at DESC), got ' || v_indexdef;
END
$$;

ROLLBACK;
"""
