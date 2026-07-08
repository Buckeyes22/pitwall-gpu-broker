"""Tests for pitwall.core.idempotency and pitwall.core.jobs.

Covers:
  - reserve_idempotency_key: fresh insert, replay, and mismatch detection
  - transition_workload: successful transition, wrong from_state, JSONB patches
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.core.idempotency import (
    IdempotencyMismatch,
    IdempotencyReservation,
    reserve_idempotency_key,
)
from pitwall.core.jobs import transition_workload

pytestmark = pytest.mark.anyio


def _hash_input(data: dict | list | None) -> str:
    if data is None:
        return hashlib.sha256(b"null").hexdigest()
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _make_conn(
    *,
    fetchrow_side_effect: list[Any] | None = None,
    execute_return: str = "UPDATE 1",
) -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.execute = AsyncMock(return_value=execute_return)
    return conn


# ---------------------------------------------------------------------------
# reserve_idempotency_key
# ---------------------------------------------------------------------------


async def test_reserve_fresh_key_returns_is_new() -> None:
    conn = _make_conn(
        fetchrow_side_effect=[
            {"workload_id": "wkl_abc"},
        ]
    )
    result = await reserve_idempotency_key(conn, key="key-1", body_hash="h1", workload_id="wkl_abc")
    assert result == IdempotencyReservation(is_new=True, workload_id="wkl_abc")
    conn.fetchrow.assert_called_once()


async def test_reserve_replay_same_body_returns_existing() -> None:
    body = {"prompt": "hello"}
    body_hash = _hash_input(body)
    conn = _make_conn(
        fetchrow_side_effect=[
            None,
            {"workload_id": "wkl_orig"},
            {"input": body},
        ]
    )
    result = await reserve_idempotency_key(
        conn, key="key-1", body_hash=body_hash, workload_id="wkl_new"
    )
    assert result == IdempotencyReservation(is_new=False, workload_id="wkl_orig")


async def test_reserve_replay_mismatched_body_raises() -> None:
    original_body = {"prompt": "hello"}
    new_hash = _hash_input({"prompt": "different"})
    conn = _make_conn(
        fetchrow_side_effect=[
            None,
            {"workload_id": "wkl_orig"},
            {"input": original_body},
        ]
    )
    with pytest.raises(IdempotencyMismatch) as exc_info:
        await reserve_idempotency_key(conn, key="key-1", body_hash=new_hash, workload_id="wkl_new")
    assert exc_info.value.original_workload_id == "wkl_orig"


async def test_reserve_replay_no_workload_row_returns_existing() -> None:
    conn = _make_conn(
        fetchrow_side_effect=[
            None,
            {"workload_id": "wkl_orig"},
            None,
        ]
    )
    result = await reserve_idempotency_key(
        conn, key="key-1", body_hash="any", workload_id="wkl_new"
    )
    assert result == IdempotencyReservation(is_new=False, workload_id="wkl_orig")


async def test_reserve_replay_null_input_returns_existing() -> None:
    conn = _make_conn(
        fetchrow_side_effect=[
            None,
            {"workload_id": "wkl_orig"},
            {"input": None},
        ]
    )
    result = await reserve_idempotency_key(
        conn, key="key-1", body_hash="any", workload_id="wkl_new"
    )
    assert result == IdempotencyReservation(is_new=False, workload_id="wkl_orig")


# ---------------------------------------------------------------------------
# transition_workload
# ---------------------------------------------------------------------------


async def test_transition_succeeds_when_in_from_state() -> None:
    conn = _make_conn(execute_return="UPDATE 1")
    ok = await transition_workload(
        conn,
        workload_id="wkl_1",
        from_states={"queued"},
        to_state="running",
    )
    assert ok is True
    sql = conn.execute.call_args[0][0]
    assert "state = $1" in sql
    assert "WHERE id = $" in sql
    assert "state IN" in sql


async def test_transition_fails_when_wrong_state() -> None:
    conn = _make_conn(execute_return="UPDATE 0")
    ok = await transition_workload(
        conn,
        workload_id="wkl_1",
        from_states={"queued"},
        to_state="running",
    )
    assert ok is False


async def test_transition_with_jsonb_patch() -> None:
    conn = _make_conn(execute_return="UPDATE 1")
    ok = await transition_workload(
        conn,
        workload_id="wkl_1",
        from_states={"running"},
        to_state="completed",
        patch={"result": {"answer": 42}, "execution_ms": 1500},
    )
    assert ok is True
    sql = conn.execute.call_args[0][0]
    assert "result = $2::jsonb" in sql
    assert "execution_ms = $3" in sql


async def test_transition_with_empty_from_states() -> None:
    conn = _make_conn(execute_return="UPDATE 0")
    ok = await transition_workload(
        conn,
        workload_id="wkl_1",
        from_states=set(),
        to_state="running",
    )
    assert ok is False


async def test_transition_multiple_from_states() -> None:
    conn = _make_conn(execute_return="UPDATE 1")
    ok = await transition_workload(
        conn,
        workload_id="wkl_1",
        from_states={"queued", "running"},
        to_state="failed",
        patch={"error": {"message": "timeout"}},
    )
    assert ok is True
    sql = conn.execute.call_args[0][0]
    assert "state IN" in sql
    args = conn.execute.call_args[0][1:]
    assert args[0] == "failed"


async def test_transition_null_patch() -> None:
    conn = _make_conn(execute_return="UPDATE 1")
    ok = await transition_workload(
        conn,
        workload_id="wkl_1",
        from_states={"queued"},
        to_state="running",
        patch=None,
    )
    assert ok is True
    sql = conn.execute.call_args[0][0]
    assert "::jsonb" not in sql
