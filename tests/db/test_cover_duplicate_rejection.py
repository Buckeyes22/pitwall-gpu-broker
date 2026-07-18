"""Schema and repository tests proving duplicate rejection.

Covers:
  - runpod_webhook_deliveries UNIQUE(runpod_job_id, attempt) constraint
  - idempotency_keys PRIMARY KEY dedup
  - reserve_idempotency_key: fresh insert, replay with matching body, mismatch detection
  - workloads partial unique index on idempotency_key
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import asyncpg
import pytest

from pitwall.core.idempotency import (
    IdempotencyMismatch,
    reserve_idempotency_key,
)

# Run under AnyIO like every other real-DB module. Without this marker the file
# fell through to pytest-asyncio's auto mode, so its async tests ran on a
# pytest-asyncio event loop while the rest of the integration suite ran on
# AnyIO's — two live loops in one session. asyncpg connections are loop-affine,
# so under CI timing a pooled connection got released/reset on the wrong loop
# ("got Future attached to a different loop" / "another operation is in
# progress"), crashing the whole integration job. One marker keeps the entire
# integration collection on a single AnyIO loop.
pytestmark = pytest.mark.anyio

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"

_ALL_MIGRATION_SQL = "\n".join(p.read_text() for p in sorted(_MIGRATION_DIR.glob("*.sql")))


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        pytest.skip("DATABASE_URL is required for cover duplicate rejection tests")
    return url


async def _make_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        _db_url(),
        min_size=1,
        max_size=4,
        init=_register_json_codec,
    )


async def _register_json_codec(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: json.dumps(v),
        decoder=lambda v: json.loads(v),
        schema="pg_catalog",
    )


def _hash_input(data: dict | list | None) -> str:
    if data is None:
        return hashlib.sha256(b"null").hexdigest()
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _seed_workload(conn: asyncpg.Connection, workload_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO pitwall.workloads
            (id, capability_id, provider_id, type, state, submitted_at)
        VALUES ($1, 'cap_cover_dup', 'prov_cover_dup', 'inference', 'queued', now())
        """,
        workload_id,
    )


async def _seed_capability_and_provider(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        INSERT INTO pitwall.capabilities
            (id, name, version, class, cost_mode, config)
        VALUES ('cap_cover_dup', 'CoverDup', 'v1', 'inference', 'per_token', '{}'::jsonb)
        ON CONFLICT (id) DO NOTHING
        """
    )
    await conn.execute(
        """
        INSERT INTO pitwall.providers
            (id, capability_id, name, provider_type, config, priority)
        VALUES ('prov_cover_dup', 'cap_cover_dup', 'CoverDupProv',
                'serverless_queue', '{}'::jsonb, 1)
        ON CONFLICT (id) DO NOTHING
        """
    )


@pytest.fixture(autouse=True)
async def _ensure_migrations() -> None:
    pool = await _make_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS pitwall CASCADE")
            await conn.execute(_ALL_MIGRATION_SQL)
            await _seed_capability_and_provider(conn)
    finally:
        await pool.close()


class TestWebhookDeliveryUniqueConstraint:
    async def test_insert_first_delivery_succeeds(self) -> None:
        pool = await _make_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO pitwall.runpod_webhook_deliveries
                        (runpod_job_id, attempt, payload)
                    VALUES ($1, $2, $3::jsonb)
                    """,
                    "job_unique_001",
                    1,
                    {"status": "COMPLETED"},
                )
                row = await conn.fetchrow(
                    "SELECT runpod_job_id, attempt FROM pitwall.runpod_webhook_deliveries "
                    "WHERE runpod_job_id = $1 AND attempt = $2",
                    "job_unique_001",
                    1,
                )
                assert row is not None
                assert row["runpod_job_id"] == "job_unique_001"
                assert row["attempt"] == 1
        finally:
            await pool.close()

    async def test_duplicate_runpod_job_id_attempt_rejected(self) -> None:
        pool = await _make_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO pitwall.runpod_webhook_deliveries
                        (runpod_job_id, attempt, payload)
                    VALUES ($1, $2, $3::jsonb)
                    """,
                    "job_dup_001",
                    1,
                    {"status": "COMPLETED"},
                )
                with pytest.raises(asyncpg.UniqueViolationError):
                    await conn.execute(
                        """
                        INSERT INTO pitwall.runpod_webhook_deliveries
                            (runpod_job_id, attempt, payload)
                        VALUES ($1, $2, $3::jsonb)
                        """,
                        "job_dup_001",
                        1,
                        {"status": "COMPLETED"},
                    )
        finally:
            await pool.close()

    async def test_same_job_id_different_attempt_succeeds(self) -> None:
        pool = await _make_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO pitwall.runpod_webhook_deliveries
                        (runpod_job_id, attempt, payload)
                    VALUES ($1, $2, $3::jsonb)
                    """,
                    "job_multi_attempt",
                    1,
                    {"status": "IN_PROGRESS"},
                )
                await conn.execute(
                    """
                    INSERT INTO pitwall.runpod_webhook_deliveries
                        (runpod_job_id, attempt, payload)
                    VALUES ($1, $2, $3::jsonb)
                    """,
                    "job_multi_attempt",
                    2,
                    {"status": "COMPLETED"},
                )
                rows = await conn.fetch(
                    "SELECT attempt FROM pitwall.runpod_webhook_deliveries "
                    "WHERE runpod_job_id = $1 ORDER BY attempt",
                    "job_multi_attempt",
                )
                assert len(rows) == 2
                assert rows[0]["attempt"] == 1
                assert rows[1]["attempt"] == 2
        finally:
            await pool.close()

    async def test_attempt_zero_rejected(self) -> None:
        pool = await _make_pool()
        try:
            async with pool.acquire() as conn:
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(
                        """
                        INSERT INTO pitwall.runpod_webhook_deliveries
                            (runpod_job_id, attempt, payload)
                        VALUES ($1, $2, $3::jsonb)
                        """,
                        "job_bad_attempt",
                        0,
                        {},
                    )
        finally:
            await pool.close()

    async def test_attempt_four_rejected(self) -> None:
        pool = await _make_pool()
        try:
            async with pool.acquire() as conn:
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(
                        """
                        INSERT INTO pitwall.runpod_webhook_deliveries
                            (runpod_job_id, attempt, payload)
                        VALUES ($1, $2, $3::jsonb)
                        """,
                        "job_bad_attempt_hi",
                        4,
                        {},
                    )
        finally:
            await pool.close()


class TestIdempotencyKeyUniqueConstraint:
    async def test_insert_fresh_key_succeeds(self) -> None:
        pool = await _make_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO pitwall.idempotency_keys (idempotency_key, workload_id)
                    VALUES ($1, $2)
                    """,
                    "idem-fresh-001",
                    "wkl_fresh_001",
                )
                row = await conn.fetchrow(
                    "SELECT idempotency_key, workload_id FROM pitwall.idempotency_keys "
                    "WHERE idempotency_key = $1",
                    "idem-fresh-001",
                )
                assert row is not None
                assert row["workload_id"] == "wkl_fresh_001"
        finally:
            await pool.close()

    async def test_duplicate_idempotency_key_rejected(self) -> None:
        pool = await _make_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO pitwall.idempotency_keys (idempotency_key, workload_id)
                    VALUES ($1, $2)
                    """,
                    "idem-dup-001",
                    "wkl_orig",
                )
                with pytest.raises(asyncpg.UniqueViolationError):
                    await conn.execute(
                        """
                        INSERT INTO pitwall.idempotency_keys (idempotency_key, workload_id)
                        VALUES ($1, $2)
                        """,
                        "idem-dup-001",
                        "wkl_other",
                    )
        finally:
            await pool.close()


class TestReserveIdempotencyKey:
    async def test_reserve_fresh_key(self) -> None:
        pool = await _make_pool()
        try:
            async with pool.acquire() as conn, conn.transaction():
                await _seed_workload(conn, "wkl_reserve_fresh")
                result = await reserve_idempotency_key(
                    conn,
                    key="reserve-fresh-key",
                    body_hash=_hash_input({"prompt": "hello"}),
                    workload_id="wkl_reserve_fresh",
                )
            assert result.is_new is True
            assert result.workload_id == "wkl_reserve_fresh"
        finally:
            await pool.close()

    async def test_reserve_replay_same_body_returns_existing(self) -> None:
        pool = await _make_pool()
        try:
            body = {"prompt": "hello world"}
            body_hash = _hash_input(body)
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await _seed_workload(conn, "wkl_replay_orig")
                    await conn.execute(
                        "UPDATE pitwall.workloads SET input = $1::jsonb WHERE id = $2",
                        body,
                        "wkl_replay_orig",
                    )
                    first = await reserve_idempotency_key(
                        conn,
                        key="replay-key",
                        body_hash=body_hash,
                        workload_id="wkl_replay_orig",
                    )
                assert first.is_new is True

                async with conn.transaction():
                    await _seed_workload(conn, "wkl_replay_new")
                    second = await reserve_idempotency_key(
                        conn,
                        key="replay-key",
                        body_hash=body_hash,
                        workload_id="wkl_replay_new",
                    )
                assert second.is_new is False
                assert second.workload_id == "wkl_replay_orig"
        finally:
            await pool.close()

    async def test_reserve_replay_mismatched_body_raises(self) -> None:
        pool = await _make_pool()
        try:
            original_body = {"prompt": "original"}
            original_hash = _hash_input(original_body)
            different_hash = _hash_input({"prompt": "different"})
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await _seed_workload(conn, "wkl_mismatch_orig")
                    await conn.execute(
                        "UPDATE pitwall.workloads SET input = $1::jsonb WHERE id = $2",
                        original_body,
                        "wkl_mismatch_orig",
                    )
                    await reserve_idempotency_key(
                        conn,
                        key="mismatch-key",
                        body_hash=original_hash,
                        workload_id="wkl_mismatch_orig",
                    )

                async with conn.transaction():
                    await _seed_workload(conn, "wkl_mismatch_new")
                    with pytest.raises(IdempotencyMismatch) as exc_info:
                        await reserve_idempotency_key(
                            conn,
                            key="mismatch-key",
                            body_hash=different_hash,
                            workload_id="wkl_mismatch_new",
                        )
                    assert exc_info.value.original_workload_id == "wkl_mismatch_orig"
        finally:
            await pool.close()


class TestWorkloadIdempotencyKeyUniqueIndex:
    async def test_duplicate_workload_idempotency_key_rejected(self) -> None:
        pool = await _make_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO pitwall.workloads
                        (id, capability_id, provider_id, type, state, submitted_at,
                         idempotency_key)
                    VALUES ($1, $2, $3, $4, $5, now(), $6)
                    """,
                    "wl_cover_1",
                    "cap_cover_dup",
                    "prov_cover_dup",
                    "inference",
                    "queued",
                    "wl-idem-cover-abc",
                )
                with pytest.raises(asyncpg.UniqueViolationError):
                    await conn.execute(
                        """
                        INSERT INTO pitwall.workloads
                            (id, capability_id, provider_id, type, state, submitted_at,
                             idempotency_key)
                        VALUES ($1, $2, $3, $4, $5, now(), $6)
                        """,
                        "wl_cover_dup",
                        "cap_cover_dup",
                        "prov_cover_dup",
                        "inference",
                        "queued",
                        "wl-idem-cover-abc",
                    )
        finally:
            await pool.close()

    async def test_multiple_null_idempotency_keys_allowed(self) -> None:
        pool = await _make_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO pitwall.workloads
                        (id, capability_id, provider_id, type, state, submitted_at)
                    VALUES ($1, $2, $3, $4, $5, now())
                    """,
                    "wl_null_idem_1",
                    "cap_cover_dup",
                    "prov_cover_dup",
                    "inference",
                    "queued",
                )
                await conn.execute(
                    """
                    INSERT INTO pitwall.workloads
                        (id, capability_id, provider_id, type, state, submitted_at)
                    VALUES ($1, $2, $3, $4, $5, now())
                    """,
                    "wl_null_idem_2",
                    "cap_cover_dup",
                    "prov_cover_dup",
                    "inference",
                    "queued",
                )
                rows = await conn.fetch(
                    "SELECT id FROM pitwall.workloads WHERE id IN ($1, $2)",
                    "wl_null_idem_1",
                    "wl_null_idem_2",
                )
                assert len(rows) == 2
        finally:
            await pool.close()
