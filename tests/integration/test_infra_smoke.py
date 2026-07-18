"""Smoke tests proving the integration fixtures wire up to real services.

Both carry @pytest.mark.integration so the default fast suite (-m "not
integration") never runs them. The PG test additionally @requires_pg so it
skips cleanly when PITWALL_TEST_DATABASE_URL is unset. They GREEN only under
``make test-int`` with docker-compose.testinfra up.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import requires_pg


@requires_pg
@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_pool_select_one(pg_pool) -> None:
    async with pg_pool.acquire() as conn:
        value = await conn.fetchval("SELECT 1")
    assert value == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redis_client_ping(redis_client) -> None:
    pong = await redis_client.ping()
    assert pong is True
