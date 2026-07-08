from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SQL_TEST = _REPO_ROOT / "tests" / "sql" / "test_canonical_gpu_constraints.sql"
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"
_TEST_POSTGRES_CONTAINER = "pitwall-test-postgres"


def test_canonical_gpu_constraint_accepts_full_names_and_rejects_shorthand() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for the Postgres GPU constraint test")

    result = _run_sql_test(database_url)

    assert result.returncode == 0, (
        "canonical GPU constraint SQL test failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _run_sql_test(database_url: str) -> subprocess.CompletedProcess[str]:
    psql = _real_host_psql(database_url)
    if psql is not None:
        return subprocess.run(
            [psql, database_url, "-v", "ON_ERROR_STOP=1", "-f", str(_SQL_TEST)],
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
        input=_sql_test_with_inlined_migrations(),
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


def _sql_test_with_inlined_migrations() -> str:
    replacements = {
        r"\ir ../../db/migrations/0001_capabilities.sql": (
            _MIGRATION_DIR / "0001_capabilities.sql"
        ).read_text(),
        r"\ir ../../db/migrations/0002_providers.sql": (
            _MIGRATION_DIR / "0002_providers.sql"
        ).read_text(),
    }
    lines: list[str] = []
    for line in _SQL_TEST.read_text().splitlines():
        replacement = replacements.get(line)
        lines.append(replacement if replacement is not None else line)
    return "\n".join(lines)
