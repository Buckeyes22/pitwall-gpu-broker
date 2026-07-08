"""Tests for cost_daily_rollup including alert hook integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.reconciler.cost_daily_rollup import run_rollup

pytestmark = pytest.mark.anyio


def _mock_conn() -> MagicMock:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


def _mock_pool() -> MagicMock:
    pool = MagicMock()
    conn = _mock_conn()
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acq)
    return pool


async def test_run_rollup_executes_aggregate_sql() -> None:
    pool = _mock_pool()
    await run_rollup(pool)
    pool.acquire.assert_called_once()
    pool.acquire.return_value.__aenter__.assert_called_once()


async def test_run_rollup_calls_after_rollup_hook() -> None:
    pool = _mock_pool()
    hook_called = False

    async def after_rollup() -> None:
        nonlocal hook_called
        hook_called = True

    await run_rollup(pool, after_rollup=after_rollup)
    assert hook_called is True


async def test_run_rollup_after_rollup_not_called_on_error() -> None:
    pool = MagicMock()
    conn = MagicMock()

    async def mock_execute(sql: str) -> None:
        raise RuntimeError("SQL error")

    conn.execute = mock_execute
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acq)

    hook_called = False

    async def after_rollup() -> None:
        nonlocal hook_called
        hook_called = True

    with pytest.raises(RuntimeError, match="SQL error"):
        await run_rollup(pool, after_rollup=after_rollup)
    assert hook_called is False
