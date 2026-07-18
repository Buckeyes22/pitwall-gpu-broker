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


def test_name_image_sha_is_unique_cache_key() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the Postgres runpod_templates migration test")

    result = _run_sql_test(database_url)

    assert result.returncode == 0, (
        "runpod_templates migration SQL test failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_runpod_templates_columns_and_defaults() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the Postgres runpod_templates migration test")

    result = _run_sql_test(database_url)

    assert result.returncode == 0, (
        "runpod_templates column/defaults test failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _run_sql_test(database_url: str) -> subprocess.CompletedProcess[str]:
    psql = _real_host_psql(database_url)
    if psql is not None:
        return subprocess.run(
            [
                psql,
                database_url,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                _build_sql(),
            ],
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


_MIGRATION_SQL = (_MIGRATION_DIR / "0005_runpod_templates.sql").read_text()


_SCHEMA_SQL = (_MIGRATION_DIR / "0001_capabilities.sql").read_text()


def _build_sql() -> str:
    return f"""\
BEGIN;
DROP SCHEMA IF EXISTS pitwall CASCADE;
{_SCHEMA_SQL}
{_MIGRATION_SQL}

-- 1. Insert a valid row
INSERT INTO pitwall.runpod_templates
  (id, runpod_template_id, name, image_sha, image_ref, registry_auth_id,
   container_disk_gb, volume_mount_path, env_schema)
VALUES
  ('tpl_001', 'rp_tpl_abc', 'pitwall-embed-bge-m3', 'sha256:aaa111',
   'gitlab-registry.example.com/pitwall/embed:sha256:aaa111', 'ra_gitlab',
   50, '/workspace', ARRAY['RUNPOD_API_KEY','PITWALL_CAPABILITY_ID']);

-- 2. Same name, different image_sha -> allowed (new image = new template)
INSERT INTO pitwall.runpod_templates
  (id, runpod_template_id, name, image_sha, image_ref)
VALUES
  ('tpl_002', 'rp_tpl_def', 'pitwall-embed-bge-m3', 'sha256:bbb222',
   'gitlab-registry.example.com/pitwall/embed:sha256:bbb222');

-- 3. Duplicate (name, image_sha) must be rejected
DO $$
BEGIN
  INSERT INTO pitwall.runpod_templates
    (id, runpod_template_id, name, image_sha, image_ref)
  VALUES
    ('tpl_003', 'rp_tpl_dup', 'pitwall-embed-bge-m3', 'sha256:aaa111',
     'gitlab-registry.example.com/pitwall/embed:sha256:aaa111');
  RAISE EXCEPTION 'expected unique violation on (name, image_sha)';
EXCEPTION WHEN unique_violation THEN
  NULL;
END
$$;

-- 4. Different name, same image_sha -> allowed
INSERT INTO pitwall.runpod_templates
  (id, runpod_template_id, name, image_sha, image_ref)
VALUES
  ('tpl_004', 'rp_tpl_ghi', 'pitwall-ocr-tesseract', 'sha256:aaa111',
   'gitlab-registry.example.com/pitwall/ocr:sha256:aaa111');

-- 5. Verify defaults: container_disk_gb=50, volume_mount_path='/workspace'
DO $$
DECLARE
  v_disk INTEGER;
  v_mount TEXT;
BEGIN
  SELECT container_disk_gb, volume_mount_path INTO v_disk, v_mount
  FROM pitwall.runpod_templates WHERE id = 'tpl_002';
  ASSERT v_disk = 50, 'expected default container_disk_gb=50, got ' || v_disk;
  ASSERT v_mount = '/workspace', 'expected default volume_mount_path=/workspace, got ' || v_mount;
END
$$;

-- 6. Verify env_schema stored correctly
DO $$
DECLARE
  v_schema TEXT[];
BEGIN
  SELECT env_schema INTO v_schema
  FROM pitwall.runpod_templates WHERE id = 'tpl_001';
  ASSERT v_schema IS NOT NULL, 'env_schema should not be NULL for tpl_001';
  ASSERT array_length(v_schema, 1) = 2, 'expected 2 env_schema entries, got ' || array_length(v_schema, 1);
END
$$;

-- 7. Verify nullable registry_auth_id
DO $$
DECLARE
  v_auth TEXT;
BEGIN
  SELECT registry_auth_id INTO v_auth
  FROM pitwall.runpod_templates WHERE id = 'tpl_002';
  ASSERT v_auth IS NULL, 'expected NULL registry_auth_id for tpl_002';
END
$$;

-- 8. Verify image_sha index exists
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_indexes
  WHERE schemaname = 'pitwall'
    AND tablename = 'runpod_templates'
    AND indexname = 'idx_runpod_templates_image_sha';
  ASSERT v_count = 1, 'expected idx_runpod_templates_image_sha index to exist';
END
$$;

-- 9. Verify the unique constraint exists
DO $$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM pg_constraint
  WHERE connamespace = 'pitwall'::regnamespace
    AND conrelid = 'pitwall.runpod_templates'::regclass
    AND contype = 'u'
    AND conname = 'runpod_templates_name_image_sha_key';
  ASSERT v_count = 1, 'expected UNIQUE(name, image_sha) constraint to exist';
END
$$;

ROLLBACK;
"""
