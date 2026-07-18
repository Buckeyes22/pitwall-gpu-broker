"""Unit coverage for the ``pitwall-gpu-broker db migrate`` asyncpg execution path."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pitwall import db
from pitwall.migrations import discover_migrations
from tests.conftest import make_asyncpg_pool


def _write_migration(path: Path, name: str, sql: str) -> str:
    migration = path / name
    migration.write_text(sql)
    return hashlib.sha256(migration.read_bytes()).hexdigest()


def _forbid_psql(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("psql migration path was used")


def test_cmd_migrate_executes_pending_sql_through_asyncpg_pool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_migration(tmp_path, "0002_second.sql", "SELECT 2;")
    _write_migration(tmp_path, "0001_first.sql", "SELECT 1;")
    pool = make_asyncpg_pool(fetch=[])

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@remote.example/db")
    monkeypatch.setattr(db, "discover_migrations", lambda: discover_migrations(tmp_path))
    monkeypatch.setattr(db, "_run_sql", _forbid_psql)
    monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(db, "close_pool", AsyncMock())

    rc = db.cmd_migrate()

    out = capsys.readouterr().out
    assert rc == 0
    assert "applied 0001_first.sql" in out
    assert "applied 0002_second.sql" in out

    executed = [call.args[0] for call in pool.conn.execute.await_args_list]
    assert executed[0] == "SELECT pg_advisory_lock($1);"
    assert any(
        statement.startswith("CREATE SCHEMA IF NOT EXISTS pitwall;") for statement in executed
    )
    assert "SELECT 1;" in executed
    assert "SELECT 2;" in executed
    record_calls = [
        call
        for call in pool.conn.execute.await_args_list
        if "INSERT INTO pitwall.schema_migrations" in call.args[0]
    ]
    assert [call.args[1] for call in record_calls] == ["0001_first", "0002_second"]


def test_cmd_migrate_skips_already_tracked_migrations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_checksum = _write_migration(tmp_path, "0001_first.sql", "SELECT 1;")
    _write_migration(tmp_path, "0002_second.sql", "SELECT 2;")
    pool = make_asyncpg_pool(fetch=[{"version": "0001_first", "checksum": first_checksum}])

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@remote.example/db")
    monkeypatch.setattr(db, "discover_migrations", lambda: discover_migrations(tmp_path))
    monkeypatch.setattr(db, "_run_sql", _forbid_psql)
    monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(db, "close_pool", AsyncMock())

    rc = db.cmd_migrate()

    out = capsys.readouterr().out
    executed = [call.args[0] for call in pool.conn.execute.await_args_list]
    assert rc == 0
    assert "applied 0001_first.sql" not in out
    assert "applied 0002_second.sql" in out
    assert "SELECT 1;" not in executed
    assert "SELECT 2;" in executed


def test_cmd_migrate_rejects_checksum_drift_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_migration(tmp_path, "0001_first.sql", "SELECT 1;")
    pool = make_asyncpg_pool(fetch=[{"version": "0001_first", "checksum": "wrong"}])
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@remote.example/db")
    monkeypatch.setattr(db, "discover_migrations", lambda: discover_migrations(tmp_path))
    monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(db, "close_pool", AsyncMock())

    assert db.cmd_migrate() == 1
    assert "checksums changed" in capsys.readouterr().err
    executed = [call.args[0] for call in pool.conn.execute.await_args_list]
    assert "SELECT pg_advisory_lock($1);" in executed
    assert "SELECT pg_advisory_unlock($1);" in executed
    assert "SELECT 1;" not in executed


def test_psql_url_credentials_are_not_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> Any:
        calls.append((args, kwargs.get("env", {})))
        stdout = "pitwall_psql_probe\n" if "-Atc" in args else ""
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    monkeypatch.setattr(db.shutil, "which", lambda command: "/usr/bin/psql")
    monkeypatch.setattr(db.subprocess, "run", fake_run)
    database_url = "postgresql://user:reserved%40password@db.example:5433/example"

    result = db._run_sql(database_url, "SELECT 1;")

    assert result.returncode == 0
    assert len(calls) == 2
    assert all(database_url not in argument for args, _ in calls for argument in args)
    assert calls[-1][1]["PGPASSWORD"] == "reserved@password"
    assert calls[-1][1]["PGHOST"] == "db.example"
