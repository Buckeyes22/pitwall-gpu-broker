"""Async repository for pitwall.rate_buckets token-bucket state.

Provides atomic load, refill, consume, and persist operations using
PostgreSQL transactions with row-level locking.
"""

from __future__ import annotations

import datetime as dt

import asyncpg

from pitwall.core.models import RateBucket
from pitwall.rate_limits.algorithm import halved_capacity

_REFILL_RATE_DIVISOR = 10.0


def _bucket_from_row(row: asyncpg.Record) -> RateBucket:
    return RateBucket(
        endpoint_id=row["endpoint_id"],
        operation=row["operation"],
        capacity=row["capacity"],
        tokens=float(row["tokens"]),
        last_refilled_at=row["last_refilled_at"],
        recent_429_at=row["recent_429_at"],
    )


class RateBucketStore:
    """Async store for pitwall.rate_buckets.

    All mutating operations are atomic and use SELECT ... FOR UPDATE
    to prevent race conditions between concurrent requests.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def load(self, endpoint_id: str, operation: str) -> RateBucket | None:
        """Load bucket state for an endpoint/operation pair.

        Returns None if the bucket does not exist.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT endpoint_id, operation, capacity, tokens,
                       last_refilled_at, recent_429_at
                FROM pitwall.rate_buckets
                WHERE endpoint_id = $1 AND operation = $2
                """,
                endpoint_id,
                operation,
            )
        if row is None:
            return None
        return _bucket_from_row(row)

    async def create(
        self,
        endpoint_id: str,
        operation: str,
        capacity: int,
        *,
        tokens: float | None = None,
        last_refilled_at: dt.datetime | None = None,
        recent_429_at: dt.datetime | None = None,
    ) -> RateBucket:
        """Create a new rate bucket with initial state.

        Tokens default to full capacity (i.e. bucket starts full).
        """
        now = dt.datetime.now(dt.UTC)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO pitwall.rate_buckets
                    (endpoint_id, operation, capacity, tokens,
                     last_refilled_at, recent_429_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (endpoint_id, operation) DO NOTHING
                RETURNING *
                """,
                endpoint_id,
                operation,
                capacity,
                tokens if tokens is not None else float(capacity),
                last_refilled_at if last_refilled_at is not None else now,
                recent_429_at,
            )
        if row is None:
            raise asyncpg.UniqueViolationError(f"Bucket already exists: {endpoint_id}/{operation}")
        return _bucket_from_row(row)

    async def load_or_create(
        self,
        endpoint_id: str,
        operation: str,
        capacity: int,
    ) -> RateBucket:
        """Load existing bucket or create a new full bucket.

        Uses INSERT ... ON CONFLICT DO UPDATE to avoid race conditions.
        """
        now = dt.datetime.now(dt.UTC)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO pitwall.rate_buckets
                    (endpoint_id, operation, capacity, tokens,
                     last_refilled_at, recent_429_at)
                VALUES ($1, $2, $3, $4, $5, NULL)
                ON CONFLICT (endpoint_id, operation) DO UPDATE
                SET endpoint_id = EXCLUDED.endpoint_id
                RETURNING *
                """,
                endpoint_id,
                operation,
                capacity,
                float(capacity),
                now,
            )
        assert row is not None
        return _bucket_from_row(row)

    async def atomic_refill_consume(
        self,
        endpoint_id: str,
        operation: str,
        tokens_to_consume: float,
    ) -> tuple[RateBucket, bool]:
        """Atomically refill tokens based on elapsed time and consume N tokens.

        Returns (bucket, allowed) where allowed is True if tokens were consumed.

        The refill rate is capacity / 10 seconds (i.e. full refill in 10s).
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                    SELECT endpoint_id, operation, capacity, tokens,
                           last_refilled_at, recent_429_at
                    FROM pitwall.rate_buckets
                    WHERE endpoint_id = $1 AND operation = $2
                    FOR UPDATE
                    """,
                endpoint_id,
                operation,
            )
            if row is None:
                raise ValueError(f"No bucket found for {endpoint_id}/{operation}")

            now = dt.datetime.now(dt.UTC)
            last = row["last_refilled_at"]
            elapsed_seconds = (now - last).total_seconds()
            refill_rate = row["capacity"] / _REFILL_RATE_DIVISOR
            new_tokens = min(
                float(row["capacity"]),
                float(row["tokens"]) + elapsed_seconds * refill_rate,
            )

            allowed = new_tokens >= tokens_to_consume
            final_tokens = new_tokens - tokens_to_consume if allowed else new_tokens

            updated = await conn.fetchrow(
                """
                    UPDATE pitwall.rate_buckets
                    SET tokens = $3,
                        last_refilled_at = $4
                    WHERE endpoint_id = $1 AND operation = $2
                    RETURNING *
                    """,
                endpoint_id,
                operation,
                final_tokens,
                now,
            )
            assert updated is not None
            return _bucket_from_row(updated), allowed

    async def record_429(
        self,
        endpoint_id: str,
        operation: str,
        new_capacity: int | None = None,
    ) -> RateBucket:
        """Record a 429 and reduce bucket capacity.

        If ``new_capacity`` is omitted, capacity is halved from the currently
        persisted value. Tokens are refilled under the old capacity, then
        clamped to the reduced capacity.
        """
        if new_capacity is not None and new_capacity <= 0:
            raise ValueError("new_capacity must be > 0")

        now = dt.datetime.now(dt.UTC)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                    SELECT endpoint_id, operation, capacity, tokens,
                           last_refilled_at, recent_429_at
                    FROM pitwall.rate_buckets
                    WHERE endpoint_id = $1 AND operation = $2
                    FOR UPDATE
                    """,
                endpoint_id,
                operation,
            )
            if row is None:
                raise ValueError(f"No bucket found for {endpoint_id}/{operation}")

            current_capacity = int(row["capacity"])
            reduced_capacity = halved_capacity(current_capacity)
            if new_capacity is not None:
                reduced_capacity = min(reduced_capacity, new_capacity)

            elapsed_seconds = (now - row["last_refilled_at"]).total_seconds()
            refill_rate = current_capacity / _REFILL_RATE_DIVISOR
            refilled_tokens = min(
                float(current_capacity),
                float(row["tokens"]) + max(0.0, elapsed_seconds) * refill_rate,
            )
            resized_tokens = min(float(reduced_capacity), refilled_tokens)

            updated = await conn.fetchrow(
                """
                    UPDATE pitwall.rate_buckets
                    SET capacity = $3,
                        tokens = $4,
                        last_refilled_at = $5,
                        recent_429_at = $5
                    WHERE endpoint_id = $1 AND operation = $2
                    RETURNING *
                    """,
                endpoint_id,
                operation,
                reduced_capacity,
                resized_tokens,
                now,
            )
            if updated is None:
                raise ValueError(f"No bucket found for {endpoint_id}/{operation}")
        return _bucket_from_row(updated)

    async def update_capacity(
        self,
        endpoint_id: str,
        operation: str,
        new_capacity: int,
    ) -> RateBucket:
        """Update bucket capacity after refilling under the previous capacity.

        Shrinks clamp tokens to the new capacity; grows keep accumulated tokens
        and let the normal 10-second refill window fill the larger bucket.
        """
        if new_capacity <= 0:
            raise ValueError("new_capacity must be > 0")

        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                    SELECT endpoint_id, operation, capacity, tokens,
                           last_refilled_at, recent_429_at
                    FROM pitwall.rate_buckets
                    WHERE endpoint_id = $1 AND operation = $2
                    FOR UPDATE
                    """,
                endpoint_id,
                operation,
            )
            if row is None:
                raise ValueError(f"No bucket found for {endpoint_id}/{operation}")

            now = dt.datetime.now(dt.UTC)
            elapsed_seconds = (now - row["last_refilled_at"]).total_seconds()
            refill_rate = row["capacity"] / _REFILL_RATE_DIVISOR
            refilled_tokens = min(
                float(row["capacity"]),
                float(row["tokens"]) + elapsed_seconds * refill_rate,
            )
            resized_tokens = min(float(new_capacity), refilled_tokens)

            updated = await conn.fetchrow(
                """
                    UPDATE pitwall.rate_buckets
                    SET capacity = $3,
                        tokens = $4,
                        last_refilled_at = $5
                    WHERE endpoint_id = $1 AND operation = $2
                    RETURNING *
                    """,
                endpoint_id,
                operation,
                new_capacity,
                resized_tokens,
                now,
            )
            if updated is None:
                raise ValueError(f"No bucket found for {endpoint_id}/{operation}")
        return _bucket_from_row(updated)

    async def persist(
        self,
        bucket: RateBucket,
    ) -> RateBucket:
        """Persist a RateBucket model back to the database."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE pitwall.rate_buckets
                SET capacity = $3,
                    tokens = $4,
                    last_refilled_at = $5,
                    recent_429_at = $6
                WHERE endpoint_id = $1 AND operation = $2
                RETURNING *
                """,
                bucket.endpoint_id,
                bucket.operation,
                bucket.capacity,
                bucket.tokens,
                bucket.last_refilled_at,
                bucket.recent_429_at,
            )
        if row is None:
            raise ValueError(f"No bucket found for {bucket.endpoint_id}/{bucket.operation}")
        return _bucket_from_row(row)


__all__ = [
    "RateBucketStore",
]
