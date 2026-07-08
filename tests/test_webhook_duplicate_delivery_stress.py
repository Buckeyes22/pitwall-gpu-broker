"""Duplicate-delivery stress test.

Post 100 duplicate webhook payloads concurrently and assert one transition
using the database state history.

The webhook receiver uses ON CONFLICT (runpod_job_id, attempt) DO NOTHING
to handle duplicate deliveries atomically at the database level. This test
verifies that 100 concurrent deliveries of the same webhook result in exactly
one row in runpod_webhook_deliveries.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import asyncpg
import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"

_ALL_MIGRATION_SQL = "\n".join(p.read_text() for p in sorted(_MIGRATION_DIR.glob("*.sql")))


def _db_url() -> str:
    url = os.getenv("PITWALL_TEST_DATABASE_URL", "") or os.getenv("DATABASE_URL", "")
    if not url:
        pytest.skip(
            "DATABASE_URL or PITWALL_TEST_DATABASE_URL is required for "
            "duplicate delivery stress test"
        )
    if "pitwall_test" not in url and "/pitwall" in url:
        url = url.replace("/pitwall", "/pitwall_test")
    return url


async def _make_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        database_url,
        min_size=1,
        max_size=10,
        init=_register_json_codec,
    )


async def _register_json_codec(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: json.dumps(v),
        decoder=lambda v: json.loads(v),
        schema="pg_catalog",
    )


@pytest.fixture(autouse=True)
async def _reset_schema() -> None:
    database_url = _db_url()
    pool = await _make_pool(database_url)
    try:
        async with pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS pitwall CASCADE")
            await conn.execute(_ALL_MIGRATION_SQL)
    finally:
        await pool.close()


@pytest.fixture
async def pool() -> asyncpg.Pool:
    database_url = _db_url()
    p = await _make_pool(database_url)
    yield p
    await p.close()


@pytest.fixture
def start_gate() -> asyncio.Event:
    """Local barrier so gathered racers hit the critical section together."""
    return asyncio.Event()


@pytest.fixture
async def webhook_app(
    pool: asyncpg.Pool,
    request: pytest.FixtureRequest,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    from pitwall.webhook_receiver import app

    app.state.pool = pool
    # lifespan (skipped by ASGITransport) normally sets this; replicate it for tests.
    app.state.redis_settings = None
    # The production limiter intentionally persists per caller. Give each test a
    # distinct caller so one stress case cannot spend the next case's allowance.
    client_address = (request.node.nodeid, 50000)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=client_address),
        base_url="http://test",
    ) as client:
        yield client


class TestDuplicateDeliveryStress:
    @pytest.mark.anyio
    async def test_concurrent_duplicate_webhooks_insert_only_one_row(
        self,
        webhook_app: httpx.AsyncClient,
        pool: asyncpg.Pool,
    ) -> None:
        job_id = "job-stress-test-001"
        webhook_payload = {"id": job_id, "status": "COMPLETED"}

        async def post_webhook() -> httpx.Response:
            return await webhook_app.post(
                "/webhooks/runpod",
                json=webhook_payload,
            )

        responses = await asyncio.gather(*[post_webhook() for _ in range(100)])

        for resp in responses:
            assert resp.status_code == 200

        duplicate_count = sum(1 for r in responses if r.json().get("duplicate"))
        new_count = sum(1 for r in responses if not r.json().get("duplicate"))

        assert new_count == 1, f"Expected exactly 1 new delivery, got {new_count}"
        assert duplicate_count == 99, f"Expected 99 duplicates, got {duplicate_count}"

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT runpod_job_id, attempt, payload, received_at
                FROM pitwall.runpod_webhook_deliveries
                WHERE runpod_job_id = $1
                ORDER BY received_at
                """,
                job_id,
            )

        assert len(rows) == 1, (
            f"Expected exactly 1 row in runpod_webhook_deliveries for job {job_id}, got {len(rows)}"
        )
        assert rows[0]["runpod_job_id"] == job_id
        assert rows[0]["attempt"] == 1

    @pytest.mark.anyio
    async def test_concurrent_duplicate_webhooks_same_attempt_deduplicated(
        self,
        webhook_app: httpx.AsyncClient,
        pool: asyncpg.Pool,
    ) -> None:
        job_id = "job-stress-attempt-002"
        webhook_payload = {"id": job_id, "status": "IN_PROGRESS"}

        async def post_webhook() -> httpx.Response:
            return await webhook_app.post(
                "/webhooks/runpod",
                json=webhook_payload,
            )

        await asyncio.gather(*[post_webhook() for _ in range(50)])

        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM pitwall.runpod_webhook_deliveries WHERE runpod_job_id = $1",
                job_id,
            )

        assert count == 1, f"Expected 1 row for attempt 1, got {count}"

    @pytest.mark.anyio
    async def test_different_attempts_all_inserted(
        self,
        webhook_app: httpx.AsyncClient,
        pool: asyncpg.Pool,
    ) -> None:
        job_id = "job-multi-attempt-003"

        async def post_webhook(attempt: int) -> httpx.Response:
            return await webhook_app.post(
                "/webhooks/runpod",
                json={"id": job_id, "status": "COMPLETED", "attempt": attempt},
                headers={"X-RunPod-Attempt": str(attempt)},
            )

        await asyncio.gather(*[post_webhook(i) for i in range(1, 4)])

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT attempt FROM pitwall.runpod_webhook_deliveries
                WHERE runpod_job_id = $1
                ORDER BY attempt
                """,
                job_id,
            )

        assert len(rows) == 3, f"Expected 3 rows for 3 different attempts, got {len(rows)}"
        attempts = [r["attempt"] for r in rows]
        assert attempts == [1, 2, 3]


class TestRepoInsertOrSkipConcurrency:
    pytestmark = [pytest.mark.anyio, pytest.mark.integration]

    async def test_same_key_collapses_to_one(
        self,
        pool: asyncpg.Pool,
        start_gate: asyncio.Event,
    ) -> None:
        from pitwall.db.repository import WebhookDeliveryRepository

        repo = WebhookDeliveryRepository(pool)
        job_id, attempt, payload = "job-repo-1", 1, {"status": "COMPLETED"}

        async def deliver() -> bool:
            await start_gate.wait()
            result = await repo.insert_or_skip(job_id, attempt, payload)
            return result.is_new

        tasks = [asyncio.create_task(deliver()) for _ in range(50)]
        start_gate.set()
        results = await asyncio.gather(*tasks)

        assert sum(1 for r in results if r) == 1
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM pitwall.runpod_webhook_deliveries WHERE runpod_job_id = $1",
                job_id,
            )
        assert count == 1

    async def test_distinct_attempts_all_insert(
        self,
        pool: asyncpg.Pool,
        start_gate: asyncio.Event,
    ) -> None:
        from pitwall.db.repository import WebhookDeliveryRepository

        repo = WebhookDeliveryRepository(pool)
        job_id, payload = "job-repo-2", {"status": "IN_PROGRESS"}

        async def deliver(attempt: int) -> bool:
            await start_gate.wait()
            result = await repo.insert_or_skip(job_id, attempt, payload)
            return result.is_new

        tasks = [asyncio.create_task(deliver(a)) for a in (1, 2, 3) for _ in range(10)]
        start_gate.set()
        results = await asyncio.gather(*tasks)

        assert sum(1 for r in results if r) == 3
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM pitwall.runpod_webhook_deliveries WHERE runpod_job_id = $1",
                job_id,
            )
        assert count == 3
