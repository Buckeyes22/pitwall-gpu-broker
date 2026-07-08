"""Polling-fallback test.

When RunPod's async webhook is dropped/lost, _poll_and_reconcile must detect
the terminal state and persist the result before RunPod's 30-minute async
result retention expires.

Tests:
  1. Dropped webhook scenario — polling catches terminal state for serverless_queue
  2. Idempotent re-poll — no-op for already-terminal workloads
  3. Redis event published after terminal state persisted
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pitwall.core.enums import WorkloadState
from pitwall.reconciler import _poll_and_reconcile

pytestmark = pytest.mark.anyio


def _make_mock_conn() -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    return conn


def _make_mock_pool() -> MagicMock:
    pool = MagicMock()
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=_make_mock_conn())
    acq.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acq)
    return pool


def _make_running_workload_row(
    workload_id: str = "wkl_poll_fallback_01",
    runpod_job_id: str = "job-poll-001",
    provider_id: str = "prov-queue-01",
    runpod_endpoint_id: str = "ep-abc123",
    provider_type: str = "serverless_queue",
) -> dict:
    return {
        "id": workload_id,
        "runpod_job_id": runpod_job_id,
        "provider_id": provider_id,
        "runpod_endpoint_id": runpod_endpoint_id,
        "provider_type": provider_type,
    }


async def test_poll_reconcile_skips_when_no_pool() -> None:
    ctx: dict = {}
    await _poll_and_reconcile(ctx)
    await _poll_and_reconcile(ctx)
    assert True


async def test_poll_reconcile_skips_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    pool = _make_mock_pool()
    ctx: dict = {"db_pool": pool, "redis": None}
    await _poll_and_reconcile(ctx)
    pool.acquire.return_value.__aenter__.return_value.fetch.assert_not_called()


async def test_poll_reconcile_queries_active_workloads() -> None:
    pool = _make_mock_pool()
    mock_conn = pool.acquire.return_value.__aenter__.return_value
    mock_conn.fetch = AsyncMock(return_value=[])

    ctx: dict = {"db_pool": pool, "redis": None}

    with patch.dict("os.environ", {"RUNPOD_API_KEY": "test-key"}):
        await _poll_and_reconcile(ctx)

    pool.acquire.return_value.__aenter__.return_value.fetch.assert_called_once()
    call_args = pool.acquire.return_value.__aenter__.return_value.fetch.call_args
    assert "pitwall.workloads" in call_args[0][0]
    assert "queued" in call_args[0][0]
    assert "running" in call_args[0][0]


async def test_poll_reconcile_skips_workload_without_endpoint_or_job_id() -> None:
    pool = _make_mock_pool()
    mock_conn = pool.acquire.return_value.__aenter__.return_value
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": "wkl-no-job",
                "runpod_job_id": None,
                "provider_id": "prov-1",
                "runpod_endpoint_id": "ep-abc",
                "provider_type": "serverless_queue",
            },
            {
                "id": "wkl-no-ep",
                "runpod_job_id": "job-123",
                "provider_id": "prov-1",
                "runpod_endpoint_id": None,
                "provider_type": "serverless_queue",
            },
        ]
    )

    ctx: dict = {"db_pool": pool, "redis": None}

    with patch.dict("os.environ", {"RUNPOD_API_KEY": "test-key"}):
        await _poll_and_reconcile(ctx)

    mock_conn.fetch.assert_called_once()


async def test_poll_reconcile_drops_webhook_polls_and_applies_terminal_state() -> None:
    pool = _make_mock_pool()
    mock_conn = pool.acquire.return_value.__aenter__.return_value

    workload_row = _make_running_workload_row()
    mock_conn.fetch = AsyncMock(return_value=[workload_row])

    completed_at = dt.datetime.now(dt.UTC)

    mock_conn.fetchrow = AsyncMock(
        return_value={
            "id": workload_row["id"],
            "capability_id": "cap_llm_qwen3_32b",
            "provider_id": workload_row["provider_id"],
            "state": WorkloadState.COMPLETED,
            "runpod_job_id": workload_row["runpod_job_id"],
            "completed_at": completed_at,
            "execution_ms": 1500,
            "output_bytes": 2048,
            "cost_actual_usd": Decimal("0.000183"),
            "error": None,
            "result": {"status": "ok"},
            "fallback_chain": None,
        }
    )

    apply_terminal_state_calls: list[dict] = []

    async def mock_apply_terminal_state(
        pool: MagicMock,
        *,
        workload_id: str,
        state: WorkloadState,
        actual_cost: Decimal | None,
        completed_at: dt.datetime,
    ) -> bool:
        apply_terminal_state_calls.append(
            {
                "workload_id": workload_id,
                "state": state,
                "actual_cost": actual_cost,
                "completed_at": completed_at,
            }
        )
        return True

    redis_mock = MagicMock()
    redis_mock.publish = MagicMock(return_value=1)

    mock_queue_job = MagicMock()
    mock_queue_job.status = "COMPLETED"

    with (
        patch.dict("os.environ", {"RUNPOD_API_KEY": "test-key"}),
        patch("pitwall.reconciler.apply_terminal_state", mock_apply_terminal_state),
        patch("pitwall.runpod_client.queue.QueueClient") as MockQueueClient,
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.status = AsyncMock(return_value=mock_queue_job)
        MockQueueClient.return_value = mock_client_instance

        await _poll_and_reconcile({"db_pool": pool, "redis": redis_mock})

    assert len(apply_terminal_state_calls) == 1
    call = apply_terminal_state_calls[0]
    assert call["workload_id"] == workload_row["id"]
    assert call["state"] == WorkloadState.COMPLETED
    assert call["completed_at"] is not None


async def test_poll_reconcile_skips_non_terminal_status() -> None:
    pool = _make_mock_pool()
    mock_conn = pool.acquire.return_value.__aenter__.return_value

    workload_row = _make_running_workload_row()
    mock_conn.fetch = AsyncMock(return_value=[workload_row])

    apply_terminal_state_calls: list[dict] = []

    async def mock_apply_terminal_state(
        pool: MagicMock,
        *,
        workload_id: str,
        state: WorkloadState,
        actual_cost: Decimal | None,
        completed_at: dt.datetime,
    ) -> bool:
        apply_terminal_state_calls.append(
            {
                "workload_id": workload_id,
                "state": state,
            }
        )
        return True

    mock_queue_job = MagicMock()
    mock_queue_job.status = "IN_PROGRESS"

    with (
        patch.dict("os.environ", {"RUNPOD_API_KEY": "test-key"}),
        patch("pitwall.reconciler.apply_terminal_state", mock_apply_terminal_state),
        patch("pitwall.runpod_client.queue.QueueClient") as MockQueueClient,
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.status = AsyncMock(return_value=mock_queue_job)
        MockQueueClient.return_value = mock_client_instance

        await _poll_and_reconcile({"db_pool": pool, "redis": None})

    assert len(apply_terminal_state_calls) == 0


async def test_poll_reconcile_idempotent_already_terminal() -> None:
    pool = _make_mock_pool()
    mock_conn = pool.acquire.return_value.__aenter__.return_value

    workload_row = _make_running_workload_row()
    mock_conn.fetch = AsyncMock(return_value=[workload_row])

    apply_terminal_state_calls: list[dict] = []

    async def mock_apply_terminal_state(
        pool: MagicMock,
        *,
        workload_id: str,
        state: WorkloadState,
        actual_cost: Decimal | None,
        completed_at: dt.datetime,
    ) -> bool:
        apply_terminal_state_calls.append(
            {
                "workload_id": workload_id,
                "state": state,
            }
        )
        return False

    mock_queue_job = MagicMock()
    mock_queue_job.status = "COMPLETED"

    with (
        patch.dict("os.environ", {"RUNPOD_API_KEY": "test-key"}),
        patch("pitwall.reconciler.apply_terminal_state", mock_apply_terminal_state),
        patch("pitwall.runpod_client.queue.QueueClient") as MockQueueClient,
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.status = AsyncMock(return_value=mock_queue_job)
        MockQueueClient.return_value = mock_client_instance

        await _poll_and_reconcile({"db_pool": pool, "redis": None})

    assert len(apply_terminal_state_calls) == 1


async def test_poll_reconcile_publishes_completed_event_when_updated() -> None:
    pool = _make_mock_pool()
    mock_conn = pool.acquire.return_value.__aenter__.return_value

    workload_row = _make_running_workload_row()
    mock_conn.fetch = AsyncMock(return_value=[workload_row])

    completed_at = dt.datetime.now(dt.UTC)

    mock_conn.fetchrow = AsyncMock(
        return_value={
            "id": workload_row["id"],
            "capability_id": "cap_llm_qwen3_32b",
            "provider_id": workload_row["provider_id"],
            "state": WorkloadState.COMPLETED,
            "runpod_job_id": workload_row["runpod_job_id"],
            "completed_at": completed_at,
            "execution_ms": 1500,
            "output_bytes": 2048,
            "cost_actual_usd": Decimal("0.000183"),
            "error": None,
            "result": {"status": "ok"},
            "fallback_chain": None,
        }
    )

    redis_mock = MagicMock()
    redis_mock.publish = MagicMock(return_value=1)

    async def mock_apply_terminal_state(
        pool: MagicMock,
        *,
        workload_id: str,
        state: WorkloadState,
        actual_cost: Decimal | None,
        completed_at: dt.datetime,
    ) -> bool:
        return True

    mock_queue_job = MagicMock()
    mock_queue_job.status = "COMPLETED"

    with (
        patch.dict("os.environ", {"RUNPOD_API_KEY": "test-key"}),
        patch("pitwall.reconciler.apply_terminal_state", mock_apply_terminal_state),
        patch("pitwall.reconciler.fetch_workload_by_id") as mock_fetch,
    ):
        mock_fetch.return_value = {
            "id": workload_row["id"],
            "capability_id": "cap_llm_qwen3_32b",
            "provider_id": workload_row["provider_id"],
            "state": WorkloadState.COMPLETED,
            "runpod_job_id": workload_row["runpod_job_id"],
            "completed_at": completed_at,
            "execution_ms": 1500,
            "output_bytes": 2048,
            "cost_actual_usd": Decimal("0.000183"),
            "error": None,
            "result": {"status": "ok"},
            "fallback_chain": None,
        }
        with patch("pitwall.runpod_client.queue.QueueClient") as MockQueueClient:
            mock_client_instance = MagicMock()
            mock_client_instance.status = AsyncMock(return_value=mock_queue_job)
            MockQueueClient.return_value = mock_client_instance

            await _poll_and_reconcile({"db_pool": pool, "redis": redis_mock})

    redis_mock.publish.assert_called_once()
    call_args = redis_mock.publish.call_args
    channel = call_args[0][0]
    payload = call_args[0][1]
    assert channel == "pitwall:workload:completed"
    assert workload_row["id"] in payload


async def test_poll_reconcile_handles_failed_status() -> None:
    pool = _make_mock_pool()
    mock_conn = pool.acquire.return_value.__aenter__.return_value

    workload_row = _make_running_workload_row()
    mock_conn.fetch = AsyncMock(return_value=[workload_row])

    apply_terminal_state_calls: list[dict] = []

    async def mock_apply_terminal_state(
        pool: MagicMock,
        *,
        workload_id: str,
        state: WorkloadState,
        actual_cost: Decimal | None,
        completed_at: dt.datetime,
    ) -> bool:
        apply_terminal_state_calls.append(
            {
                "workload_id": workload_id,
                "state": state,
            }
        )
        return True

    mock_queue_job = MagicMock()
    mock_queue_job.status = "FAILED"
    mock_queue_job.error = "Out of memory"

    with (
        patch.dict("os.environ", {"RUNPOD_API_KEY": "test-key"}),
        patch("pitwall.reconciler.apply_terminal_state", mock_apply_terminal_state),
        patch("pitwall.runpod_client.queue.QueueClient") as MockQueueClient,
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.status = AsyncMock(return_value=mock_queue_job)
        MockQueueClient.return_value = mock_client_instance

        await _poll_and_reconcile({"db_pool": pool, "redis": None})

    assert len(apply_terminal_state_calls) == 1
    assert apply_terminal_state_calls[0]["state"] == WorkloadState.FAILED


async def test_poll_reconcile_handles_cancelled_status() -> None:
    pool = _make_mock_pool()
    mock_conn = pool.acquire.return_value.__aenter__.return_value

    workload_row = _make_running_workload_row()
    mock_conn.fetch = AsyncMock(return_value=[workload_row])

    apply_terminal_state_calls: list[dict] = []

    async def mock_apply_terminal_state(
        pool: MagicMock,
        *,
        workload_id: str,
        state: WorkloadState,
        actual_cost: Decimal | None,
        completed_at: dt.datetime,
    ) -> bool:
        apply_terminal_state_calls.append(
            {
                "workload_id": workload_id,
                "state": state,
            }
        )
        return True

    mock_queue_job = MagicMock()
    mock_queue_job.status = "CANCELLED"

    with (
        patch.dict("os.environ", {"RUNPOD_API_KEY": "test-key"}),
        patch("pitwall.reconciler.apply_terminal_state", mock_apply_terminal_state),
        patch("pitwall.runpod_client.queue.QueueClient") as MockQueueClient,
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.status = AsyncMock(return_value=mock_queue_job)
        MockQueueClient.return_value = mock_client_instance

        await _poll_and_reconcile({"db_pool": pool, "redis": None})

    assert len(apply_terminal_state_calls) == 1
    assert apply_terminal_state_calls[0]["state"] == WorkloadState.CANCELLED


async def test_poll_reconcile_handles_timed_out_status() -> None:
    pool = _make_mock_pool()
    mock_conn = pool.acquire.return_value.__aenter__.return_value

    workload_row = _make_running_workload_row()
    mock_conn.fetch = AsyncMock(return_value=[workload_row])

    apply_terminal_state_calls: list[dict] = []

    async def mock_apply_terminal_state(
        pool: MagicMock,
        *,
        workload_id: str,
        state: WorkloadState,
        actual_cost: Decimal | None,
        completed_at: dt.datetime,
    ) -> bool:
        apply_terminal_state_calls.append(
            {
                "workload_id": workload_id,
                "state": state,
            }
        )
        return True

    mock_queue_job = MagicMock()
    mock_queue_job.status = "TIMED_OUT"

    with (
        patch.dict("os.environ", {"RUNPOD_API_KEY": "test-key"}),
        patch("pitwall.reconciler.apply_terminal_state", mock_apply_terminal_state),
        patch("pitwall.runpod_client.queue.QueueClient") as MockQueueClient,
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.status = AsyncMock(return_value=mock_queue_job)
        MockQueueClient.return_value = mock_client_instance

        await _poll_and_reconcile({"db_pool": pool, "redis": None})

    assert len(apply_terminal_state_calls) == 1
    assert apply_terminal_state_calls[0]["state"] == WorkloadState.TIMED_OUT


async def test_poll_reconcile_continues_on_api_exception() -> None:
    pool = _make_mock_pool()
    mock_conn = pool.acquire.return_value.__aenter__.return_value

    workload_row_1 = _make_running_workload_row(workload_id="wkl-1", runpod_job_id="job-1")
    workload_row_2 = _make_running_workload_row(workload_id="wkl-2", runpod_job_id="job-2")
    mock_conn.fetch = AsyncMock(return_value=[workload_row_1, workload_row_2])

    apply_terminal_state_calls: list[dict] = []

    async def mock_apply_terminal_state(
        pool: MagicMock,
        *,
        workload_id: str,
        state: WorkloadState,
        actual_cost: Decimal | None,
        completed_at: dt.datetime,
    ) -> bool:
        apply_terminal_state_calls.append(
            {
                "workload_id": workload_id,
                "state": state,
            }
        )
        return True

    mock_queue_job = MagicMock()
    mock_queue_job.status = "COMPLETED"

    call_count = 0

    async def status_side_effect(endpoint_id: str, job_id: str) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.HTTPStatusError(
                "Server error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            )
        return mock_queue_job

    with (
        patch.dict("os.environ", {"RUNPOD_API_KEY": "test-key"}),
        patch("pitwall.reconciler.apply_terminal_state", mock_apply_terminal_state),
        patch("pitwall.runpod_client.queue.QueueClient") as MockQueueClient,
    ):
        mock_client_instance = MagicMock()
        mock_client_instance.status = AsyncMock(side_effect=status_side_effect)
        MockQueueClient.return_value = mock_client_instance

        await _poll_and_reconcile({"db_pool": pool, "redis": None})

    assert len(apply_terminal_state_calls) == 1
    assert apply_terminal_state_calls[0]["workload_id"] == "wkl-2"


async def test_poll_reconcile_handles_pod_lease_provider_type() -> None:
    pool = _make_mock_pool()
    mock_conn = pool.acquire.return_value.__aenter__.return_value

    workload_row = _make_running_workload_row(
        provider_type="pod_lease",
        runpod_endpoint_id=None,
    )
    mock_conn.fetch = AsyncMock(return_value=[workload_row])

    apply_terminal_state_calls: list[dict] = []

    async def mock_apply_terminal_state(
        pool: MagicMock,
        *,
        workload_id: str,
        state: WorkloadState,
        actual_cost: Decimal | None,
        completed_at: dt.datetime,
    ) -> bool:
        apply_terminal_state_calls.append(
            {
                "workload_id": workload_id,
                "state": state,
            }
        )
        return True

    with (
        patch.dict("os.environ", {"RUNPOD_API_KEY": "test-key"}),
        patch("pitwall.reconciler.apply_terminal_state", mock_apply_terminal_state),
        patch("pitwall.runpod_client.pods.get_pod") as mock_get_pod,
    ):
        mock_get_pod.return_value = {
            "id": workload_row["runpod_job_id"],
            "runtime": {
                "podStatus": "RUNNING",
            },
        }

        await _poll_and_reconcile({"db_pool": pool, "redis": None})

    assert len(apply_terminal_state_calls) == 0


async def test_poll_reconcile_pod_timed_out_when_no_pod() -> None:
    pool = _make_mock_pool()
    mock_conn = pool.acquire.return_value.__aenter__.return_value

    workload_row = _make_running_workload_row(
        provider_type="pod_lease",
        runpod_endpoint_id="pod-lease-01",
    )
    mock_conn.fetch = AsyncMock(return_value=[workload_row])

    apply_terminal_state_calls: list[dict] = []

    async def mock_apply_terminal_state(
        pool: MagicMock,
        *,
        workload_id: str,
        state: WorkloadState,
        actual_cost: Decimal | None,
        completed_at: dt.datetime,
    ) -> bool:
        apply_terminal_state_calls.append(
            {
                "workload_id": workload_id,
                "state": state,
            }
        )
        return True

    with (
        patch.dict("os.environ", {"RUNPOD_API_KEY": "test-key"}),
        patch("pitwall.reconciler.apply_terminal_state", mock_apply_terminal_state),
        patch("pitwall.runpod_client.pods.get_pod") as mock_get_pod,
    ):
        mock_get_pod.return_value = None

        await _poll_and_reconcile({"db_pool": pool, "redis": None})

    assert len(apply_terminal_state_calls) == 1
    assert apply_terminal_state_calls[0]["state"] == WorkloadState.TIMED_OUT
