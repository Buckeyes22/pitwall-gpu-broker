"""Tests for the workload lifecycle module.

Insert and update workload rows for pass-through requests without
changing the external OpenAI response.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pitwall.core.enums import WorkloadState
from pitwall.core.models import Workload
from pitwall.workload_lifecycle import (
    generate_workload_id,
    insert_passthrough_workload,
    transition_to_completed,
    transition_to_failed,
    transition_to_running,
)

_TEST_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)


def _make_workload(
    *,
    id: str = "wkl_test01",
    state: WorkloadState = WorkloadState.QUEUED,
    capability_id: str = "cap_llm_qwen3_32b",
    provider_id: str = "prov_qwen3_32b",
    **overrides: Any,
) -> Workload:
    defaults: dict[str, Any] = {
        "id": id,
        "capability_id": capability_id,
        "provider_id": provider_id,
        "type": "openai_passthrough",
        "state": state,
        "submitted_at": _TEST_NOW,
    }
    defaults.update(overrides)
    return Workload(**defaults)


def _make_workload_repo() -> AsyncMock:
    repo = AsyncMock()
    return repo


class TestGenerateWorkloadId:
    def test_generates_prefixed_ulid(self) -> None:
        wid = generate_workload_id()
        assert wid.startswith("wkl_")
        ulid_part = wid[4:]
        assert len(ulid_part) == 26

    def test_generates_unique_ids(self) -> None:
        ids = {generate_workload_id() for _ in range(100)}
        assert len(ids) == 100


class TestInsertPassthroughWorkload:
    @pytest.mark.anyio
    async def test_inserts_with_queued_state(self) -> None:
        repo = _make_workload_repo()
        expected = _make_workload()
        repo.insert.return_value = expected

        result = await insert_passthrough_workload(
            repo,
            workload_id="wkl_test01",
            capability_id="cap_llm_qwen3_32b",
            provider_id="prov_qwen3_32b",
        )

        assert result == expected
        repo.insert.assert_called_once()
        inserted: Workload = repo.insert.call_args[0][0]
        assert inserted.id == "wkl_test01"
        assert inserted.state == WorkloadState.QUEUED
        assert inserted.type == "openai_passthrough"
        assert inserted.capability_id == "cap_llm_qwen3_32b"
        assert inserted.provider_id == "prov_qwen3_32b"

    @pytest.mark.anyio
    async def test_inserts_with_optional_fields(self) -> None:
        repo = _make_workload_repo()
        expected = _make_workload()
        repo.insert.return_value = expected

        await insert_passthrough_workload(
            repo,
            workload_id="wkl_test02",
            capability_id="cap_llm_qwen3_32b",
            provider_id="prov_qwen3_32b",
            idempotency_key="idem-123",
            input_data={"model": "qwen3-32b-awq", "messages": []},
            input_bytes=256,
        )

        inserted: Workload = repo.insert.call_args[0][0]
        assert inserted.idempotency_key == "idem-123"
        assert inserted.input == {"model": "qwen3-32b-awq", "messages": []}
        assert inserted.input_bytes == 256


class TestTransitionToRunning:
    @pytest.mark.anyio
    async def test_transitions_from_queued_to_running(self) -> None:
        repo = _make_workload_repo()
        running_workload = _make_workload(state=WorkloadState.RUNNING)
        repo.guarded_transition.return_value = running_workload

        result = await transition_to_running(repo, "wkl_test01")

        assert result == running_workload
        call_args = repo.guarded_transition.call_args
        assert call_args[0][0] == "wkl_test01"
        assert call_args[1]["from_states"] == {WorkloadState.QUEUED}
        assert call_args[1]["to_state"] == WorkloadState.RUNNING
        assert "started_at" in call_args[1]["patch"]

    @pytest.mark.anyio
    async def test_passes_fallback_chain(self) -> None:
        repo = _make_workload_repo()
        repo.guarded_transition.return_value = _make_workload(state=WorkloadState.RUNNING)

        await transition_to_running(repo, "wkl_test01", fallback_chain=["prov_a", "prov_b"])

        call_args = repo.guarded_transition.call_args
        assert call_args[1]["patch"]["fallback_chain"] == ["prov_a", "prov_b"]

    @pytest.mark.anyio
    async def test_duplicate_transition_returns_none(self) -> None:
        repo = _make_workload_repo()
        repo.guarded_transition.return_value = None

        result = await transition_to_running(repo, "wkl_already_running")

        assert result is None
        repo.guarded_transition.assert_called_once()

    @pytest.mark.anyio
    async def test_only_queued_state_allowed(self) -> None:
        repo = _make_workload_repo()
        repo.guarded_transition.return_value = _make_workload(state=WorkloadState.RUNNING)

        await transition_to_running(repo, "wkl_test01")

        call_args = repo.guarded_transition.call_args
        assert call_args[1]["from_states"] == {WorkloadState.QUEUED}

    @pytest.mark.anyio
    async def test_second_call_on_same_workload_returns_none(self) -> None:
        repo = _make_workload_repo()
        running_workload = _make_workload(state=WorkloadState.RUNNING)
        repo.guarded_transition.side_effect = [running_workload, None]

        result1 = await transition_to_running(repo, "wkl_test01")
        result2 = await transition_to_running(repo, "wkl_test01")

        assert result1 == running_workload
        assert result2 is None


class TestTransitionToCompleted:
    @pytest.mark.anyio
    async def test_transitions_to_completed(self) -> None:
        repo = _make_workload_repo()
        completed_workload = _make_workload(state=WorkloadState.COMPLETED)
        repo.guarded_transition.return_value = completed_workload

        result = await transition_to_completed(
            repo,
            "wkl_test01",
            execution_ms=150,
            output_bytes=2048,
        )

        assert result == completed_workload
        call_kwargs = repo.guarded_transition.call_args
        assert call_kwargs[0][0] == "wkl_test01"
        assert call_kwargs[1]["from_states"] == {WorkloadState.RUNNING}
        assert call_kwargs[1]["to_state"] == WorkloadState.COMPLETED
        assert call_kwargs[1]["patch"]["execution_ms"] == 150
        assert call_kwargs[1]["patch"]["output_bytes"] == 2048
        assert "completed_at" in call_kwargs[1]["patch"]

    @pytest.mark.anyio
    async def test_passes_result_and_fallback_chain(self) -> None:
        repo = _make_workload_repo()
        repo.guarded_transition.return_value = _make_workload(state=WorkloadState.COMPLETED)

        await transition_to_completed(
            repo,
            "wkl_test01",
            result={"status": "ok"},
            fallback_chain=["prov_a"],
            langfuse_trace_id="trace-123",
        )

        call_kwargs = repo.guarded_transition.call_args
        assert call_kwargs[1]["patch"]["result"] == {"status": "ok"}
        assert call_kwargs[1]["patch"]["fallback_chain"] == ["prov_a"]
        assert call_kwargs[1]["patch"]["langfuse_trace_id"] == "trace-123"


class TestTransitionToFailed:
    @pytest.mark.anyio
    async def test_transitions_to_failed(self) -> None:
        repo = _make_workload_repo()
        failed_workload = _make_workload(state=WorkloadState.FAILED)
        repo.guarded_transition.return_value = failed_workload

        result = await transition_to_failed(
            repo,
            "wkl_test01",
            execution_ms=5000,
            error={"error": "timeout"},
        )

        assert result == failed_workload
        call_kwargs = repo.guarded_transition.call_args
        assert call_kwargs[0][0] == "wkl_test01"
        assert call_kwargs[1]["from_states"] == {WorkloadState.RUNNING}
        assert call_kwargs[1]["to_state"] == WorkloadState.FAILED
        assert call_kwargs[1]["patch"]["execution_ms"] == 5000
        assert call_kwargs[1]["patch"]["error"] == {"error": "timeout"}
        assert "completed_at" in call_kwargs[1]["patch"]

    @pytest.mark.anyio
    async def test_passes_fallback_chain_and_trace(self) -> None:
        repo = _make_workload_repo()
        repo.guarded_transition.return_value = _make_workload(state=WorkloadState.FAILED)

        await transition_to_failed(
            repo,
            "wkl_test01",
            fallback_chain=["prov_a", "prov_b"],
            langfuse_trace_id="trace-456",
        )

        call_kwargs = repo.guarded_transition.call_args
        assert call_kwargs[1]["patch"]["fallback_chain"] == ["prov_a", "prov_b"]
        assert call_kwargs[1]["patch"]["langfuse_trace_id"] == "trace-456"


class TestFullLifecycle:
    @pytest.mark.anyio
    async def test_full_lifecycle_queued_to_completed(self) -> None:
        repo = _make_workload_repo()

        repo.insert.return_value = _make_workload(state=WorkloadState.QUEUED)
        repo.guarded_transition.return_value = _make_workload(state=WorkloadState.RUNNING)

        async def guarded_side_effect(*args: Any, **kwargs: Any) -> Workload:
            if kwargs.get("to_state") == WorkloadState.RUNNING:
                return _make_workload(state=WorkloadState.RUNNING)
            if kwargs.get("to_state") == WorkloadState.COMPLETED:
                return _make_workload(state=WorkloadState.COMPLETED)
            return _make_workload(state=WorkloadState.QUEUED)

        repo.guarded_transition.side_effect = guarded_side_effect

        workload = await insert_passthrough_workload(
            repo,
            workload_id="wkl_lifecycle",
            capability_id="cap_llm_qwen3_32b",
            provider_id="prov_qwen3_32b",
            input_bytes=100,
        )
        assert workload.state == WorkloadState.QUEUED

        workload = await transition_to_running(repo, "wkl_lifecycle")
        assert workload.state == WorkloadState.RUNNING

        workload = await transition_to_completed(
            repo, "wkl_lifecycle", execution_ms=200, output_bytes=500
        )
        assert workload.state == WorkloadState.COMPLETED

    @pytest.mark.anyio
    async def test_full_lifecycle_queued_to_failed(self) -> None:
        repo = _make_workload_repo()

        repo.insert.return_value = _make_workload(state=WorkloadState.QUEUED)

        async def guarded_side_effect(*args: Any, **kwargs: Any) -> Workload:
            if kwargs.get("to_state") == WorkloadState.RUNNING:
                return _make_workload(state=WorkloadState.RUNNING)
            if kwargs.get("to_state") == WorkloadState.FAILED:
                return _make_workload(state=WorkloadState.FAILED)
            return _make_workload(state=WorkloadState.QUEUED)

        repo.guarded_transition.side_effect = guarded_side_effect

        workload = await insert_passthrough_workload(
            repo,
            workload_id="wkl_fail",
            capability_id="cap_llm_qwen3_32b",
            provider_id="prov_qwen3_32b",
        )
        assert workload.state == WorkloadState.QUEUED

        workload = await transition_to_running(repo, "wkl_fail")
        assert workload.state == WorkloadState.RUNNING

        workload = await transition_to_failed(repo, "wkl_fail", error={"error": "upstream 500"})
        assert workload.state == WorkloadState.FAILED
