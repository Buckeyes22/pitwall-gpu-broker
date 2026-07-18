"""Session/function fixtures for tests needing real Postgres/Redis.

Backed by docker-compose.testinfra.yml (PG 127.0.0.1:5444 db pitwall_test
user/pw pitwall/pitwall; Redis 127.0.0.1:6380). Gated on PITWALL_TEST_DATABASE_URL
and PITWALL_TEST_REDIS_URL.

Schema bootstrap: there is no runtime ``apply_migrations`` in this codebase —
``pitwall.migrations`` only does discovery/drift. We discover the on-disk
``db/migrations/*.sql`` via ``discover_migrations`` and run them in order, the
same way the existing ``_pg`` tests apply schema.

JSONB codec: the production pool registers a text json codec so JSONB columns
(config / input / result / endpoints / readiness) decode to dict/list rather
than str; without it the repository ``_*_from_row`` decoders see strings and
fail. The ``pg_pool`` fixture registers the same codec via ``init=``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from pitwall.db import _register_codecs
from pitwall.migrations import discover_migrations

PG_URL = os.getenv("PITWALL_TEST_DATABASE_URL", "")
REDIS_URL = os.getenv("PITWALL_TEST_REDIS_URL", "redis://127.0.0.1:6380/0")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"

requires_pg = pytest.mark.skipif(not PG_URL, reason="PITWALL_TEST_DATABASE_URL not set")


def _all_migration_sql() -> str:
    """Concatenate every db/migrations/*.sql in discover_migrations order."""
    records = discover_migrations(_MIGRATION_DIR)
    return "\n".join((_MIGRATION_DIR / r.filename).read_text() for r in records)


async def _register_json_codec(conn: asyncpg.Connection) -> None:
    """Use the production jsonb codec verbatim so tests can't drift from it."""
    await _register_codecs(conn)


async def _apply_all_migrations(conn: asyncpg.Connection) -> None:
    await conn.execute("DROP SCHEMA IF EXISTS pitwall CASCADE")
    await conn.execute(_all_migration_sql())


# The integration tests use pytest-asyncio, matching the repository-wide
# ``asyncio_mode = "auto"`` setting. Keeping the fixtures and tests on the same
# runner prevents asyncpg and Redis clients from crossing event loops.
@pytest.fixture
async def pg_pool() -> AsyncIterator[asyncpg.Pool]:
    """Fresh pitwall schema (drop + migrate) per test, backed by a real pool."""
    if not PG_URL:
        pytest.skip("PITWALL_TEST_DATABASE_URL not set")
    pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=4, init=_register_json_codec)
    assert pool is not None
    async with pool.acquire() as conn:
        await _apply_all_migrations(conn)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def redis_client() -> AsyncIterator[Any]:
    """A real redis.asyncio client with a flushed db, per test."""
    import redis.asyncio as redis

    client = redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def start_gate() -> asyncio.Event:
    """An Event all racers await before the critical section.

    Lets every gathered coroutine reach lock acquisition together, maximizing
    real contention on the Postgres advisory/row lock.
    """
    return asyncio.Event()
