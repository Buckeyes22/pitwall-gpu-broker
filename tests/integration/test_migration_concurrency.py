"""Concurrent installed migration runners serialize without partial state."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

pytestmark = pytest.mark.integration


async def test_two_migration_runners_serialize(pg_pool: Any) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS pitwall CASCADE")

    environment = os.environ.copy()
    environment["DATABASE_URL"] = environment["PITWALL_TEST_DATABASE_URL"]

    async def run() -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pitwall",
            "db",
            "migrate",
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        return process.returncode or 0, stdout.decode(), stderr.decode()

    results = await asyncio.gather(run(), run())
    assert [result[0] for result in results] == [0, 0], results

    async with pg_pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM pitwall.schema_migrations")
        duplicates = await conn.fetchval(
            """
            SELECT count(*) FROM (
                SELECT version FROM pitwall.schema_migrations
                GROUP BY version HAVING count(*) > 1
            ) AS duplicate_versions
            """
        )
    assert count == 21
    assert duplicates == 0
