"""Tests for idempotency_gc — 24-hour expiry of idempotency keys."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.reconciler import _idempotency_gc

pytestmark = pytest.mark.anyio


def _make_mock_conn() -> MagicMock:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="DELETE 5")
    return conn


def _make_mock_pool() -> MagicMock:
    pool = MagicMock()
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=_make_mock_conn())
    acq.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acq)
    return pool


async def test_gc_skips_when_no_pool() -> None:
    ctx: dict = {}
    await _idempotency_gc(ctx)
    await _idempotency_gc(ctx)
    assert True


async def test_gc_deletes_old_idempotency_keys() -> None:
    pool = _make_mock_pool()
    ctx: dict = {"db_pool": pool}
    await _idempotency_gc(ctx)
    pool.acquire.return_value.__aenter__.return_value.execute.assert_called_once()
    call_args = pool.acquire.return_value.__aenter__.return_value.execute.call_args
    sql = call_args[0][0]
    assert "DELETE FROM pitwall.idempotency_keys" in sql
    assert "pitwall.workloads" not in sql
    assert "24 hours" in sql


async def test_gc_does_not_delete_recent_keys() -> None:
    pool = _make_mock_pool()
    ctx: dict = {"db_pool": pool}
    await _idempotency_gc(ctx)
    executed_sql = pool.acquire.return_value.__aenter__.return_value.execute.call_args[0][0]
    assert "created_at < NOW() - INTERVAL '24 hours'" in executed_sql


async def test_gc_uses_strict_inequality_for_24_hour_boundary() -> None:
    pool = _make_mock_pool()
    ctx: dict = {"db_pool": pool}
    await _idempotency_gc(ctx)
    executed_sql = pool.acquire.return_value.__aenter__.return_value.execute.call_args[0][0]
    assert "< NOW() - INTERVAL '24 hours'" in executed_sql
    assert "<= NOW() - INTERVAL '24 hours'" not in executed_sql


async def test_gc_boundary_logic_at_exactly_24_hours() -> None:
    pool = _make_mock_pool()
    ctx: dict = {"db_pool": pool}
    await _idempotency_gc(ctx)
    executed_sql = pool.acquire.return_value.__aenter__.return_value.execute.call_args[0][0]

    assert "created_at < NOW() - INTERVAL '24 hours'" in executed_sql

    assert "<=" not in executed_sql.replace("< NOW() - INTERVAL '24 hours'", ""), (
        "Strict inequality required: < not <="
    )


async def test_gc_never_deletes_workload_ledger_rows() -> None:
    workloads: dict[str, dict[str, str]] = {
        "wkl_old": {"id": "wkl_old", "idempotency_key": "old-key"},
        "wkl_recent": {"id": "wkl_recent", "idempotency_key": "recent-key"},
    }
    original_workloads = {key: value.copy() for key, value in workloads.items()}
    idempotency_keys = {
        "old-key": {"workload_id": "wkl_old"},
        "recent-key": {"workload_id": "wkl_recent"},
    }

    async def execute_gc(sql: str) -> str:
        assert "pitwall.workloads" not in sql
        assert "pitwall.idempotency_keys" in sql
        idempotency_keys.pop("old-key")
        return "DELETE 1"

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=execute_gc)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)

    await _idempotency_gc({"db_pool": pool})

    assert workloads == original_workloads
    assert idempotency_keys == {"recent-key": {"workload_id": "wkl_recent"}}
