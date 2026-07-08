"""Migration index verification via asyncpg.

Applies all migrations in a disposable schema and verifies that every expected
index exists in the pitwall schema by querying pg_indexes directly through
asyncpg instead of shelling out to psql.
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"

_ALL_MIGRATION_SQL = "\n".join(p.read_text() for p in sorted(_MIGRATION_DIR.glob("*.sql")))

_EXPECTED_INDEXES = [
    ("providers", "idx_providers_capability_priority"),
    ("providers", "idx_providers_cooldown_until"),
    ("workloads", "idx_workloads_idempotency"),
    ("workloads", "idx_workloads_state_submitted"),
    ("workloads", "idx_workloads_month_spend"),
    ("leases", "idx_leases_expires"),
    ("runpod_templates", "idx_runpod_templates_image_sha"),
    ("kill_log", "idx_kill_log_triggered"),
    ("config_audit", "idx_audit_entity"),
    ("volumes", "idx_volumes_datacenter"),
]


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        pytest.skip("DATABASE_URL is required for migration index tests")
    return url


@pytest.fixture(autouse=True)
async def _setup_schema() -> None:
    conn = await asyncpg.connect(_db_url())
    try:
        await conn.execute("DROP SCHEMA IF EXISTS pitwall CASCADE")
        await conn.execute(_ALL_MIGRATION_SQL)
    finally:
        await conn.close()


class TestMigrationIndexes:
    @pytest.mark.asyncio
    async def test_expected_indexes_exist(self) -> None:
        conn = await asyncpg.connect(_db_url())
        try:
            rows = await conn.fetch(
                "SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'pitwall'"
            )
            found = {(r["tablename"], r["indexname"]) for r in rows}
            for table, idx in _EXPECTED_INDEXES:
                assert (table, idx) in found, f"expected index {idx} on pitwall.{table} not found"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_at_least_nine_non_pk_indexes(self) -> None:
        conn = await asyncpg.connect(_db_url())
        try:
            rows = await conn.fetch(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'pitwall' AND indexname NOT LIKE '%_pkey'"
            )
            assert len(rows) >= 10, f"expected at least 10 non-PK indexes, found {len(rows)}"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_all_migrations_create_tables_in_pitwall_schema(
        self,
    ) -> None:
        conn = await asyncpg.connect(_db_url())
        try:
            rows = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'pitwall' AND table_type = 'BASE TABLE'"
            )
            tables = {r["table_name"] for r in rows}
            expected_tables = {
                "capabilities",
                "providers",
                "workloads",
                "leases",
                "runpod_templates",
                "kill_log",
                "config_audit",
                "volumes",
                "cost_daily",
                "rate_buckets",
                "alert_events",
            }
            assert expected_tables.issubset(tables), f"missing tables: {expected_tables - tables}"
        finally:
            await conn.close()
