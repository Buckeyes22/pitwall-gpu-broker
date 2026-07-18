"""Real Postgres backup/restore acceptance, including URL-reserved credentials."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import pytest

pytestmark = pytest.mark.integration


def _password_url(database_url: str, password: str) -> str:
    parsed = urlsplit(database_url)
    username = quote(parsed.username or "pitwall", safe="")
    host = parsed.hostname or "127.0.0.1"
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit(
        (
            parsed.scheme,
            f"{username}:{quote(password, safe='')}@{host}{port}",
            parsed.path,
            parsed.query,
            "",
        )
    )


async def test_backup_restore_compares_all_tables_with_reserved_password(
    pg_pool: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for command in ("psql", "pg_dump", "pg_restore"):
        if shutil.which(command) is None:
            pytest.skip(f"{command} is required for the backup/restore integration test")

    from pitwall.ops.backup_drill import PITWALL_TABLES, run_pit_restore_drill

    database_url = os.environ["PITWALL_TEST_DATABASE_URL"]
    reserved_password = "p@ss/word:with%reserved"
    async with pg_pool.acquire() as conn:
        await conn.execute(f"ALTER ROLE pitwall PASSWORD '{reserved_password}'")
    monkeypatch.setenv("PITWALL_DATABASE_URL", _password_url(database_url, reserved_password))
    monkeypatch.setenv("PITWALL_DRILL_ARTIFACTS_DIR", str(tmp_path))
    try:
        report = await run_pit_restore_drill(
            {"db_pool": pg_pool},
            target="integration-reserved-password",
        )
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("ALTER ROLE pitwall PASSWORD 'pitwall'")

    assert report.passed, report.errors
    checked_tables = {check.table for check in report.checks}
    assert set(PITWALL_TABLES) <= checked_tables
    assert {"retention_runs", "webhook_subscriptions"} <= checked_tables
    assert all(check.passed for check in report.checks)
    assert not any("reserved" in error for error in report.errors)
