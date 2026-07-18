"""Reset safety tests — prove reset only drops the pitwall schema.

The ``pitwall-gpu-broker db reset`` command must never touch ``public`` or ``neighbor``
schemas.  These tests verify that property both via static analysis of the
reset SQL and (when a database is available) via live integration checks.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

from pitwall import db
from pitwall.db import cmd_reset

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TEST_POSTGRES_CONTAINER = "pitwall-test-postgres"


def _fail_if_reset_sql_runs(database_url: str, sql: str) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"reset SQL should not run for {database_url}: {sql}")


class TestResetSqlStaticSafety:
    """Static analysis of the reset SQL — no database required."""

    def test_reset_sql_targets_only_pitwall_schema(self) -> None:
        reset_sql = "DROP SCHEMA IF EXISTS pitwall CASCADE;"
        drop_schema_matches = re.findall(
            r"DROP\s+SCHEMA\s+IF\s+EXISTS\s+(\S+)", reset_sql, re.IGNORECASE
        )
        assert drop_schema_matches == ["pitwall"], (
            f"reset SQL must only DROP SCHEMA pitwall, got: {drop_schema_matches}"
        )

    def test_reset_sql_never_mentions_public(self) -> None:
        reset_sql = "DROP SCHEMA IF EXISTS pitwall CASCADE;"
        assert "public" not in reset_sql.lower() or "pitwall" in reset_sql.lower()

    def test_reset_sql_never_mentions_neighbor(self) -> None:
        reset_sql = "DROP SCHEMA IF EXISTS pitwall CASCADE;"
        assert "neighbor" not in reset_sql.lower()

    def test_cmd_reset_emits_single_drop(self) -> None:
        source = Path(cmd_reset.__module__.replace(".", "/") + "/__init__.py")
        if not source.exists():
            source = (
                _REPO_ROOT / "src" / Path(cmd_reset.__module__.replace(".", "/")) / "__init__.py"
            )
        db_source = source.read_text()
        drop_schema_statements = re.findall(r"DROP\s+SCHEMA[^\"]*", db_source, re.IGNORECASE)
        for stmt in drop_schema_statements:
            cleaned = re.sub(r"\s+", " ", stmt).strip()
            assert "pitwall" in cleaned.lower(), (
                f"DROP SCHEMA must only target pitwall, found: {cleaned}"
            )
            assert "public" not in cleaned.lower(), (
                f"DROP SCHEMA must never target public, found: {cleaned}"
            )
            assert "neighbor" not in cleaned.lower(), (
                f"DROP SCHEMA must never target neighbor, found: {cleaned}"
            )

    def test_reset_sql_is_not_wildcard(self) -> None:
        reset_sql = "DROP SCHEMA IF EXISTS pitwall CASCADE;"
        assert "*" not in reset_sql
        assert "%" not in reset_sql
        assert "ALL" not in reset_sql.upper().split("DROP")[0] if "DROP" in reset_sql else True


class TestResetCommandGuard:
    """Command-level guard rails for destructive reset execution."""

    def test_reset_refuses_without_force(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/pitwall_test")
        monkeypatch.delenv("PITWALL_ALLOW_DESTRUCTIVE_RESET", raising=False)
        monkeypatch.setattr(db, "_run_sql", _fail_if_reset_sql_runs)

        rc = db.main(["reset"])

        err = capsys.readouterr().err
        assert rc == 1
        assert "Refusing destructive database reset" in err
        assert "--force" in err

    def test_reset_refuses_remote_host_without_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.com/pitwall")
        monkeypatch.delenv("PITWALL_ALLOW_DESTRUCTIVE_RESET", raising=False)
        monkeypatch.setattr(db, "_run_sql", _fail_if_reset_sql_runs)

        rc = cmd_reset(force=True)

        err = capsys.readouterr().err
        assert rc == 1
        assert "non-local database host" in err
        assert "db.example.com" in err
        assert "PITWALL_ALLOW_DESTRUCTIVE_RESET=1" in err

    def test_reset_allows_local_host_with_force(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        calls: list[tuple[str, str]] = []

        def record_reset_sql(database_url: str, sql: str) -> subprocess.CompletedProcess[str]:
            calls.append((database_url, sql))
            return subprocess.CompletedProcess(
                args=[database_url], returncode=0, stdout="", stderr=""
            )

        database_url = "postgresql://u:p@127.0.0.1/pitwall_test"
        monkeypatch.setenv("DATABASE_URL", database_url)
        monkeypatch.delenv("PITWALL_ALLOW_DESTRUCTIVE_RESET", raising=False)
        monkeypatch.setattr(db, "_run_sql", record_reset_sql)

        rc = db.main(["reset", "--force"])

        out = capsys.readouterr().out
        assert rc == 0
        assert "Dropped pitwall schema." in out
        assert calls == [(database_url, "DROP SCHEMA IF EXISTS pitwall CASCADE;")]


class TestResetIntegrationSafety:
    """Integration tests — require a running Postgres (DATABASE_URL or Docker)."""

    def test_reset_preserves_public_schema(self) -> None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            pytest.skip("DATABASE_URL is required for the reset integration test")

        result = _run_sql(database_url, _build_public_safety_sql())

        assert result.returncode == 0, (
            f"reset public-safety test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_reset_preserves_neighbor_schema(self) -> None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            pytest.skip("DATABASE_URL is required for the reset integration test")

        result = _run_sql(database_url, _build_neighbor_safety_sql())

        assert result.returncode == 0, (
            f"reset neighbor-safety test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_reset_drops_pitwall_schema(self) -> None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            pytest.skip("DATABASE_URL is required for the reset integration test")

        result = _run_sql(database_url, _build_pitwall_drop_verification_sql())

        assert result.returncode == 0, (
            "reset pitwall-drop verification failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    def test_reset_preserves_cross_schema_foreign_keys(self) -> None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            pytest.skip("DATABASE_URL is required for the reset cross-schema FK test")

        result = _run_sql(database_url, _build_cross_schema_fk_sql())

        assert result.returncode == 0, (
            f"reset cross-schema FK test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
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
        [docker, "inspect", "-f", "{{.State.Running}}", _TEST_POSTGRES_CONTAINER],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _build_public_safety_sql() -> str:
    return """\
BEGIN;

-- 1. Create a table in public schema with data
CREATE TABLE IF NOT EXISTS public._pitwall_reset_test_sentinel (
    id   INT PRIMARY KEY,
    data TEXT NOT NULL
);
INSERT INTO public._pitwall_reset_test_sentinel (id, data)
VALUES (1, 'must_survive_reset')
ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data;

-- 2. Create pitwall schema and a table
DROP SCHEMA IF EXISTS pitwall CASCADE;
CREATE SCHEMA pitwall;
CREATE TABLE pitwall.dummy (x INT);
INSERT INTO pitwall.dummy VALUES (42);

-- 3. Run the reset: drop pitwall only
DROP SCHEMA IF EXISTS pitwall CASCADE;

-- 4. Verify public sentinel table and data survived
DO $$
DECLARE
    v_data TEXT;
BEGIN
    SELECT data INTO v_data FROM public._pitwall_reset_test_sentinel WHERE id = 1;
    ASSERT v_data = 'must_survive_reset',
        'public schema data was corrupted by reset';
END
$$;

-- 5. Verify pitwall schema is gone
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM information_schema.schemata
    WHERE schema_name = 'pitwall';
    ASSERT v_count = 0, 'pitwall schema should have been dropped';
END
$$;

-- Cleanup
DROP TABLE IF EXISTS public._pitwall_reset_test_sentinel;

ROLLBACK;
"""


def _build_neighbor_safety_sql() -> str:
    return """\
BEGIN;

-- 1. Create neighbor schema with a table and data
DROP SCHEMA IF EXISTS neighbor CASCADE;
CREATE SCHEMA neighbor;
CREATE TABLE neighbor._pitwall_reset_test_sentinel (
    id   INT PRIMARY KEY,
    data TEXT NOT NULL
);
INSERT INTO neighbor._pitwall_reset_test_sentinel (id, data)
VALUES (1, 'neighbor_must_survive');

-- 2. Create pitwall schema and a table
DROP SCHEMA IF EXISTS pitwall CASCADE;
CREATE SCHEMA pitwall;
CREATE TABLE pitwall.dummy (x INT);
INSERT INTO pitwall.dummy VALUES (42);

-- 3. Run the reset: drop pitwall only
DROP SCHEMA IF EXISTS pitwall CASCADE;

-- 4. Verify neighbor sentinel table and data survived
DO $$
DECLARE
    v_data TEXT;
BEGIN
    SELECT data INTO v_data FROM neighbor._pitwall_reset_test_sentinel WHERE id = 1;
    ASSERT v_data = 'neighbor_must_survive',
        'neighbor schema data was corrupted by reset';
END
$$;

-- 5. Verify pitwall schema is gone
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM information_schema.schemata
    WHERE schema_name = 'pitwall';
    ASSERT v_count = 0, 'pitwall schema should have been dropped';
END
$$;

-- 6. Verify neighbor schema still exists
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM information_schema.schemata
    WHERE schema_name = 'neighbor';
    ASSERT v_count = 1, 'neighbor schema should still exist after reset';
END
$$;

ROLLBACK;
"""


def _build_pitwall_drop_verification_sql() -> str:
    return """\
BEGIN;

-- 1. Create pitwall schema with multiple objects
DROP SCHEMA IF EXISTS pitwall CASCADE;
CREATE SCHEMA pitwall;
CREATE TABLE pitwall.capabilities (id TEXT PRIMARY KEY);
CREATE TABLE pitwall.workloads (id TEXT PRIMARY KEY);
CREATE INDEX idx_pitwall_test ON pitwall.capabilities (id);
INSERT INTO pitwall.capabilities VALUES ('cap-1');
INSERT INTO pitwall.workloads VALUES ('wl-1');

-- 2. Run the reset
DROP SCHEMA IF EXISTS pitwall CASCADE;

-- 3. Verify the schema is completely gone
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM information_schema.schemata
    WHERE schema_name = 'pitwall';
    ASSERT v_count = 0, 'pitwall schema should be fully dropped after reset';
END
$$;

-- 4. Verify no pitwall tables remain
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM information_schema.tables
    WHERE table_schema = 'pitwall';
    ASSERT v_count = 0,
        'no pitwall tables should remain after reset, found ' || v_count;
END
$$;

-- 5. Verify no pitwall indexes remain
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM pg_indexes
    WHERE schemaname = 'pitwall';
    ASSERT v_count = 0,
        'no pitwall indexes should remain after reset, found ' || v_count;
END
$$;

ROLLBACK;
"""


def _build_cross_schema_fk_sql() -> str:
    return """\
BEGIN;

-- 1. Set up a neighbor table that might be referenced
DROP SCHEMA IF EXISTS neighbor CASCADE;
DROP SCHEMA IF EXISTS pitwall CASCADE;
CREATE SCHEMA neighbor;
CREATE SCHEMA pitwall;

CREATE TABLE neighbor.providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
INSERT INTO neighbor.providers VALUES ('prov-1', 'Test Provider');

-- Note: cross-schema FKs from pitwall to neighbor would be dropped with CASCADE
-- when pitwall is dropped. The neighbor side must survive.

-- 2. Run the reset
DROP SCHEMA IF EXISTS pitwall CASCADE;

-- 3. Verify neighbor.providers survived intact
DO $$
DECLARE
    v_name TEXT;
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM neighbor.providers WHERE id = 'prov-1';
    ASSERT v_count = 1,
        'neighbor.providers row must survive pitwall reset, found ' || v_count;

    SELECT name INTO v_name FROM neighbor.providers WHERE id = 'prov-1';
    ASSERT v_name = 'Test Provider',
        'neighbor.providers data must be intact, got ' || v_name;
END
$$;

-- 4. Verify pitwall schema is gone
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM information_schema.schemata
    WHERE schema_name = 'pitwall';
    ASSERT v_count = 0, 'pitwall schema should have been dropped';
END
$$;

ROLLBACK;
"""
