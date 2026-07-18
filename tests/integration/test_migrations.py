"""release program Task 1: migration integrity against real Postgres.

Proves the on-disk db/migrations/*.sql apply cleanly and idempotently, that
discover_migrations() matches the files on disk, and that a drop -> re-migrate
cycle reproduces the same schema. Uses the live pg_pool fixture.
"""

from __future__ import annotations

import pytest

from pitwall.migrations import discover_migrations
from tests.integration.conftest import (
    _MIGRATION_DIR,
    _all_migration_sql,
    requires_pg,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, requires_pg]


async def _base_tables(conn) -> set[str]:
    rows = await conn.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'pitwall' AND table_type = 'BASE TABLE'"
    )
    return {r["table_name"] for r in rows}


async def test_discover_matches_disk() -> None:
    records = discover_migrations(_MIGRATION_DIR)
    disk = sorted(p.name for p in _MIGRATION_DIR.glob("*.sql"))
    assert [r.filename for r in records] == disk
    assert len(records) == 21
    # versions are unique and ascending in discovery order
    versions = [r.version for r in records]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)


async def test_all_migrations_apply_clean(pg_pool) -> None:
    # pg_pool already applied every migration onto a fresh schema.
    async with pg_pool.acquire() as conn:
        tables = await _base_tables(conn)
    # 0001..0021 create well more than 10 base tables.
    assert len(tables) >= 10
    assert "capabilities" in tables
    assert "providers" in tables
    assert "workloads" in tables
    assert "leases" in tables
    assert "retention_runs" in tables


async def test_migrations_are_apply_once_not_idempotent(pg_pool) -> None:
    """Characterize: the raw migration SQL is apply-ONCE, not re-runnable.

    FINDING: 0001 uses bare ``CREATE TABLE`` (no IF NOT EXISTS), so re-running the
    full SQL on an already-migrated schema raises DuplicateTableError. Migrations
    are meant to be tracked/applied once, so this is acceptable — but it is pinned
    here so any future move to idempotent DDL is a conscious change. The
    drop->re-migrate path (next test) is the supported way to rebuild.
    """
    import asyncpg

    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.DuplicateTableError):
            await conn.execute(_all_migration_sql())


async def test_drop_then_remigrate_reproduces_schema(pg_pool) -> None:
    async with pg_pool.acquire() as conn:
        original = await _base_tables(conn)
        await conn.execute("DROP SCHEMA IF EXISTS pitwall CASCADE")
        await conn.execute(_all_migration_sql())
        rebuilt = await _base_tables(conn)
    assert rebuilt == original
