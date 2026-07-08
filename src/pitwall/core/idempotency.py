"""Atomic idempotency-key reservation for Pitwall async jobs.

Provides ``reserve_idempotency_key`` which atomically maps a client-supplied
Idempotency-Key to a workload_id inside a transaction.  Replay of the same key
with matching body hash returns the original workload; mismatched body hash
raises :class:`IdempotencyMismatch`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class IdempotencyReservation:
    """Result of an idempotency-key reservation attempt."""

    is_new: bool
    workload_id: str


class IdempotencyMismatch(Exception):
    """Raised when an idempotency key is reused with a different body hash."""

    def __init__(self, original_workload_id: str) -> None:
        self.original_workload_id = original_workload_id
        super().__init__(
            f"idempotency_mismatch: key already bound to workload {original_workload_id}"
        )


def _hash_input(input_data: dict[str, object] | list[object] | None) -> str:
    if input_data is None:
        return hashlib.sha256(b"null").hexdigest()
    return hashlib.sha256(
        json.dumps(input_data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


_INSERT_SQL = """
    INSERT INTO pitwall.idempotency_keys (idempotency_key, workload_id)
    VALUES ($1, $2)
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING workload_id
"""

_LOOKUP_SQL = """
    SELECT workload_id FROM pitwall.idempotency_keys
    WHERE idempotency_key = $1
"""

_WORKLOAD_INPUT_SQL = """
    SELECT input FROM pitwall.workloads WHERE id = $1
"""


async def reserve_idempotency_key(
    conn: asyncpg.Connection,
    *,
    key: str,
    body_hash: str,
    workload_id: str,
) -> IdempotencyReservation:
    """Atomically reserve an idempotency key mapping.

    If *key* is new, inserts ``(key, workload_id)`` into
    ``pitwall.idempotency_keys`` and returns
    ``IdempotencyReservation(is_new=True, ...)``.

    If *key* already exists the caller's *body_hash* is compared against the
    stored workload's input.  A matching hash means this is a safe replay and
    the existing ``workload_id`` is returned with ``is_new=False``.  A
    mismatched hash raises :class:`IdempotencyMismatch` (the API layer maps
    this to HTTP 422).
    """
    row = await conn.fetchrow(_INSERT_SQL, key, workload_id)
    if row is not None:
        return IdempotencyReservation(is_new=True, workload_id=workload_id)

    existing = await conn.fetchrow(_LOOKUP_SQL, key)
    assert existing is not None, f"idempotency key {key!r} not found after INSERT conflict"
    existing_workload_id: str = existing["workload_id"]

    workload_row = await conn.fetchrow(_WORKLOAD_INPUT_SQL, existing_workload_id)
    if workload_row is not None and workload_row["input"] is not None:
        stored_hash = _hash_input(workload_row["input"])
        if stored_hash != body_hash:
            raise IdempotencyMismatch(existing_workload_id)

    return IdempotencyReservation(is_new=False, workload_id=existing_workload_id)


__all__ = [
    "IdempotencyMismatch",
    "IdempotencyReservation",
    "reserve_idempotency_key",
]
