"""Operator CLI coverage for bounded retention execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pitwall.retention import __main__ as retention_cli


class _Pool:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_retention_cli_forwards_bounds_flags_and_prints_manifest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    pool = _Pool()
    captured: dict[str, Any] = {}

    async def create_pool(dsn: str, *, min_size: int, max_size: int) -> _Pool:
        captured.update(dsn=dsn, min_size=min_size, max_size=max_size)
        return pool

    async def archive(received_pool: _Pool, archive_dir: Path, **kwargs: Any) -> dict[str, Any]:
        captured.update(pool=received_pool, archive_dir=archive_dir, **kwargs)
        return {"status": "dry_run", "selected_count": 4}

    monkeypatch.setenv("DATABASE_URL", "postgresql://operator@db/pitwall")
    monkeypatch.setattr(retention_cli.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(retention_cli, "archive_workloads_to_jsonl", archive)

    result = retention_cli.main(
        [
            "run",
            "--archive-dir",
            str(tmp_path),
            "--days",
            "30",
            "--batch-size",
            "25",
            "--purge",
            "--dry-run",
        ]
    )

    assert result == 0
    assert captured == {
        "dsn": "postgresql://operator@db/pitwall",
        "min_size": 1,
        "max_size": 2,
        "pool": pool,
        "archive_dir": tmp_path,
        "older_than_days": 30,
        "batch_size": 25,
        "purge": True,
        "dry_run": True,
    }
    assert pool.closed is True
    assert capsys.readouterr().out.strip() == '{"selected_count": 4, "status": "dry_run"}'


def test_retention_cli_requires_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        retention_cli.main(["run", "--archive-dir", str(tmp_path)])
    assert exc_info.value.code == 2


def test_retention_cli_closes_pool_when_archive_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pool = _Pool()

    async def create_pool(_dsn: str, *, min_size: int, max_size: int) -> _Pool:
        assert (min_size, max_size) == (1, 2)
        return pool

    async def archive(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("archive failed")

    monkeypatch.setenv("DATABASE_URL", "postgresql://operator@db/pitwall")
    monkeypatch.setattr(retention_cli.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(retention_cli, "archive_workloads_to_jsonl", archive)

    with pytest.raises(RuntimeError, match="archive failed"):
        retention_cli.main(["run", "--archive-dir", str(tmp_path)])
    assert pool.closed is True
