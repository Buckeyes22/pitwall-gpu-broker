"""Tests for WorkloadRepository _workload_from_row and DB operations.

Validate the repository row mapper and the mock-asyncpg-pool
integration for insert, get, and update_state.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from pitwall.core.enums import WorkloadState
from pitwall.core.models import Workload
from pitwall.db.repository import WorkloadRepository, _workload_from_row
from tests.conftest import make_asyncpg_pool

_TEST_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)


class _Row(dict):
    """Dict subclass that also supports attribute access (mimics asyncpg.Record)."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _workload_row(**overrides: Any) -> _Row:
    defaults: dict[str, Any] = {
        "id": "wkl_test01",
        "capability_id": "cap_llm_qwen3_32b",
        "provider_id": "prov_qwen3_32b",
        "type": "openai_passthrough",
        "state": "queued",
        "runpod_job_id": None,
        "idempotency_key": None,
        "input": None,
        "result": None,
        "fallback_chain": None,
        "error": None,
        "submitted_at": _TEST_NOW,
        "started_at": None,
        "completed_at": None,
        "execution_ms": None,
        "queue_ms": None,
        "cold_start_ms": None,
        "input_bytes": None,
        "output_bytes": None,
        "cost_estimate_usd": None,
        "cost_actual_usd": None,
        "langfuse_trace_id": None,
    }
    defaults.update(overrides)
    return _Row(defaults)


def _make_workload(**overrides: Any) -> Workload:
    defaults: dict[str, Any] = {
        "id": "wkl_test01",
        "capability_id": "cap_llm_qwen3_32b",
        "provider_id": "prov_qwen3_32b",
        "type": "openai_passthrough",
        "state": WorkloadState.QUEUED,
        "submitted_at": _TEST_NOW,
    }
    defaults.update(overrides)
    return Workload(**defaults)


class TestWorkloadFromRow:
    def test_maps_basic_row(self) -> None:
        row = _workload_row()
        wl = _workload_from_row(row)
        assert wl.id == "wkl_test01"
        assert wl.capability_id == "cap_llm_qwen3_32b"
        assert wl.provider_id == "prov_qwen3_32b"
        assert wl.type == "openai_passthrough"
        assert wl.state == "queued"
        assert wl.submitted_at == _TEST_NOW

    def test_maps_row_with_all_fields(self) -> None:
        row = _workload_row(
            state="running",
            runpod_job_id="job_abc",
            idempotency_key="idem-123",
            input={"model": "qwen3-32b-awq"},
            result={"choices": []},
            fallback_chain=["prov_a", "prov_b"],
            error=None,
            started_at=_TEST_NOW,
            execution_ms=150,
            queue_ms=10,
            cold_start_ms=500,
            input_bytes=256,
            output_bytes=1024,
            cost_estimate_usd="0.001000",
            cost_actual_usd="0.002000",
            langfuse_trace_id="trace-789",
        )
        wl = _workload_from_row(row)
        assert wl.runpod_job_id == "job_abc"
        assert wl.idempotency_key == "idem-123"
        assert wl.input == {"model": "qwen3-32b-awq"}
        assert wl.fallback_chain == ["prov_a", "prov_b"]
        assert wl.execution_ms == 150
        assert wl.queue_ms == 10
        assert wl.cold_start_ms == 500
        assert wl.input_bytes == 256
        assert wl.output_bytes == 1024


class TestWorkloadRepositoryInsert:
    @pytest.mark.anyio
    async def test_insert_returns_workload(self) -> None:
        row = _workload_row()
        pool = make_asyncpg_pool(fetchrow=row)
        repo = WorkloadRepository(pool)

        workload = _make_workload()
        result = await repo.insert(workload)

        assert result.id == "wkl_test01"
        assert result.capability_id == "cap_llm_qwen3_32b"

    @pytest.mark.anyio
    async def test_insert_passes_all_fields(self) -> None:
        row = _workload_row()
        pool = make_asyncpg_pool(fetchrow=row)
        repo = WorkloadRepository(pool)

        workload = _make_workload(
            runpod_job_id="job_xyz",
            idempotency_key="idem-456",
            input_bytes=100,
        )
        await repo.insert(workload)

        conn = pool.conn
        call_args = conn.fetchrow.call_args
        assert call_args[0][1] == "wkl_test01"
        assert call_args[0][2] == "cap_llm_qwen3_32b"
        assert call_args[0][6] == "job_xyz"
        assert call_args[0][7] == "idem-456"
        assert call_args[0][18] == 100


class TestWorkloadRepositoryGet:
    @pytest.mark.anyio
    async def test_get_returns_workload_when_found(self) -> None:
        row = _workload_row()
        pool = make_asyncpg_pool(fetchrow=row)
        repo = WorkloadRepository(pool)

        result = await repo.get("wkl_test01")
        assert result is not None
        assert result.id == "wkl_test01"

    @pytest.mark.anyio
    async def test_get_returns_none_when_not_found(self) -> None:
        pool = make_asyncpg_pool(fetchrow=None)
        repo = WorkloadRepository(pool)

        result = await repo.get("wkl_nonexistent")
        assert result is None


class TestWorkloadRepositoryGetByIdempotencyKey:
    @pytest.mark.anyio
    async def test_finds_by_key(self) -> None:
        row = _workload_row(idempotency_key="idem-unique")
        pool = make_asyncpg_pool(fetchrow=row)
        repo = WorkloadRepository(pool)

        result = await repo.get_by_idempotency_key("idem-unique")
        assert result is not None
        assert result.idempotency_key == "idem-unique"

    @pytest.mark.anyio
    async def test_returns_none_when_not_found(self) -> None:
        pool = make_asyncpg_pool(fetchrow=None)
        repo = WorkloadRepository(pool)

        result = await repo.get_by_idempotency_key("nonexistent")
        assert result is None


class TestWorkloadRepositoryUpdateState:
    @pytest.mark.anyio
    async def test_update_state_basic(self) -> None:
        row = _workload_row(state="running", started_at=_TEST_NOW)
        pool = make_asyncpg_pool(fetchrow=row)
        repo = WorkloadRepository(pool)

        result = await repo.update_state("wkl_test01", WorkloadState.RUNNING)
        assert result is not None
        assert result.state == "running"

    @pytest.mark.anyio
    async def test_update_state_with_all_fields(self) -> None:
        row = _workload_row(
            state="completed",
            started_at=_TEST_NOW,
            completed_at=_TEST_NOW,
            execution_ms=200,
            output_bytes=1024,
            result={"status": "ok"},
            fallback_chain=["prov_a"],
            langfuse_trace_id="trace-123",
        )
        pool = make_asyncpg_pool(fetchrow=row)
        repo = WorkloadRepository(pool)

        result = await repo.update_state(
            "wkl_test01",
            WorkloadState.COMPLETED,
            started_at=_TEST_NOW,
            completed_at=_TEST_NOW,
            execution_ms=200,
            output_bytes=1024,
            result={"status": "ok"},
            fallback_chain=["prov_a"],
            langfuse_trace_id="trace-123",
        )
        assert result is not None
        assert result.state == "completed"

    @pytest.mark.anyio
    async def test_update_state_returns_none_when_not_found(self) -> None:
        pool = make_asyncpg_pool(fetchrow=None)
        repo = WorkloadRepository(pool)

        result = await repo.update_state("wkl_nonexistent", WorkloadState.RUNNING)
        assert result is None

    @pytest.mark.anyio
    async def test_update_state_to_failed_with_error(self) -> None:
        row = _workload_row(
            state="failed",
            completed_at=_TEST_NOW,
            execution_ms=5000,
            error={"error": "upstream timeout"},
        )
        pool = make_asyncpg_pool(fetchrow=row)
        repo = WorkloadRepository(pool)

        result = await repo.update_state(
            "wkl_test01",
            WorkloadState.FAILED,
            completed_at=_TEST_NOW,
            execution_ms=5000,
            error={"error": "upstream timeout"},
        )
        assert result is not None
        assert result.state == "failed"


class TestWorkloadRepositoryGuardedTransition:
    @pytest.mark.anyio
    async def test_guarded_transition_succeeds_from_correct_state(self) -> None:
        row = _workload_row(state="running", started_at=_TEST_NOW)
        pool = make_asyncpg_pool(fetchrow=row)
        repo = WorkloadRepository(pool)

        result = await repo.guarded_transition(
            "wkl_test01",
            from_states={"queued"},
            to_state=WorkloadState.RUNNING,
            patch={"started_at": _TEST_NOW},
        )
        assert result is not None
        assert result.state == "running"

        conn = pool.conn
        execute_calls = conn.execute.call_args_list
        assert any("FOR UPDATE" in str(c) for c in execute_calls)

        fetchrow_calls = conn.fetchrow.call_args_list
        assert any("state IN" in str(c) for c in fetchrow_calls)

    @pytest.mark.anyio
    async def test_guarded_transition_returns_none_when_wrong_state(self) -> None:
        pool = make_asyncpg_pool(fetchrow=None)
        repo = WorkloadRepository(pool)

        result = await repo.guarded_transition(
            "wkl_test01",
            from_states={"queued"},
            to_state=WorkloadState.RUNNING,
        )
        assert result is None

    @pytest.mark.anyio
    async def test_guarded_transition_uses_row_lock(self) -> None:
        row = _workload_row(state="running", started_at=_TEST_NOW)
        pool = make_asyncpg_pool(fetchrow=row)
        repo = WorkloadRepository(pool)

        await repo.guarded_transition(
            "wkl_test01",
            from_states={"queued"},
            to_state=WorkloadState.RUNNING,
        )

        conn = pool.conn
        execute_call = conn.execute.call_args
        assert "FOR UPDATE" in execute_call[0][0]

    @pytest.mark.anyio
    async def test_guarded_transition_query_checks_from_states(self) -> None:
        row = _workload_row(state="running", started_at=_TEST_NOW)
        pool = make_asyncpg_pool(fetchrow=row)
        repo = WorkloadRepository(pool)

        await repo.guarded_transition(
            "wkl_test01",
            from_states={"queued"},
            to_state=WorkloadState.RUNNING,
            patch={"started_at": _TEST_NOW},
        )

        conn = pool.conn
        fetchrow_call = conn.fetchrow.call_args
        sql = fetchrow_call[0][0]
        assert "state IN" in sql
        assert "queued" in str(fetchrow_call[0][1:])

    @pytest.mark.anyio
    async def test_guarded_transition_with_fallback_chain(self) -> None:
        row = _workload_row(state="running", started_at=_TEST_NOW)
        pool = make_asyncpg_pool(fetchrow=row)
        repo = WorkloadRepository(pool)

        result = await repo.guarded_transition(
            "wkl_test01",
            from_states={"queued"},
            to_state=WorkloadState.RUNNING,
            patch={"started_at": _TEST_NOW, "fallback_chain": ["prov_a", "prov_b"]},
        )
        assert result is not None

    @pytest.mark.anyio
    async def test_guarded_transition_runs_in_transaction(self) -> None:
        row = _workload_row(state="running", started_at=_TEST_NOW)
        pool = make_asyncpg_pool(fetchrow=row)
        repo = WorkloadRepository(pool)

        await repo.guarded_transition(
            "wkl_test01",
            from_states={"queued"},
            to_state=WorkloadState.RUNNING,
        )

        conn = pool.conn
        conn.transaction.assert_called_once()
