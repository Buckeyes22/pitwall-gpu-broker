"""FastAPI route handlers for the Jobs read and cancel surface.

GET    /v1/jobs/{id}          — full workload row
GET    /v1/jobs/{id}/status   — state summary
GET    /v1/jobs/{id}/result   — persisted result (409 if non-terminal)
POST   /v1/jobs/{id}/cancel   — cancel queued/running jobs
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from pitwall.api.exceptions import JobNotReady, RateLimited, WorkloadNotFound
from pitwall.api.schemas.params import PathId
from pitwall.core.enums import WorkloadState
from pitwall.db.repository import WorkloadRepository
from pitwall.runpod_client.queue import QueueClient

router = APIRouter()

_TERMINAL_STATES = frozenset(
    {
        WorkloadState.COMPLETED.value,
        WorkloadState.FAILED.value,
        WorkloadState.CANCELLED.value,
        WorkloadState.TIMED_OUT.value,
    }
)

_CANCEL_IDEMPOTENT_STATES = frozenset(
    {
        WorkloadState.CANCELLED.value,
        WorkloadState.COMPLETED.value,
        WorkloadState.FAILED.value,
        WorkloadState.TIMED_OUT.value,
    }
)


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return pool


def _workload_repo(request: Request) -> WorkloadRepository:
    return WorkloadRepository(_pool(request))


def _serialize_workload(w: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": w.id,
        "capability_id": w.capability_id,
        "provider_id": w.provider_id,
        "type": w.type,
        "state": w.state.value if hasattr(w.state, "value") else w.state,
        "runpod_job_id": w.runpod_job_id,
        "idempotency_key": w.idempotency_key,
        "input": w.input,
        "result": w.result,
        "fallback_chain": w.fallback_chain,
        "error": w.error,
        "submitted_at": w.submitted_at.isoformat() if w.submitted_at else None,
        "started_at": w.started_at.isoformat() if w.started_at else None,
        "completed_at": w.completed_at.isoformat() if w.completed_at else None,
        "execution_ms": w.execution_ms,
        "queue_ms": w.queue_ms,
        "cold_start_ms": w.cold_start_ms,
        "input_bytes": w.input_bytes,
        "output_bytes": w.output_bytes,
        "cost_estimate_usd": str(w.cost_estimate_usd) if w.cost_estimate_usd is not None else None,
        "cost_actual_usd": str(w.cost_actual_usd) if w.cost_actual_usd is not None else None,
        "langfuse_trace_id": w.langfuse_trace_id,
    }
    return payload


@router.get("/v1/jobs/{workload_id}")
async def get_job(workload_id: PathId, request: Request) -> dict[str, Any]:
    repo = _workload_repo(request)
    workload = await repo.get(workload_id)
    if workload is None:
        raise WorkloadNotFound(workload_id)
    return _serialize_workload(workload)


@router.get("/v1/jobs/{workload_id}/status")
async def get_job_status(workload_id: PathId, request: Request) -> dict[str, Any]:
    repo = _workload_repo(request)
    workload = await repo.get(workload_id)
    if workload is None:
        raise WorkloadNotFound(workload_id)
    state = workload.state.value if hasattr(workload.state, "value") else workload.state
    return {
        "id": workload.id,
        "state": state,
        "runpod_job_id": workload.runpod_job_id,
        "submitted_at": workload.submitted_at.isoformat() if workload.submitted_at else None,
        "started_at": workload.started_at.isoformat() if workload.started_at else None,
        "completed_at": workload.completed_at.isoformat() if workload.completed_at else None,
        "error": workload.error,
    }


@router.get("/v1/jobs/{workload_id}/result")
async def get_job_result(workload_id: PathId, request: Request) -> dict[str, Any]:
    repo = _workload_repo(request)
    workload = await repo.get(workload_id)
    if workload is None:
        raise WorkloadNotFound(workload_id)
    state = workload.state.value if hasattr(workload.state, "value") else workload.state
    if state not in _TERMINAL_STATES:
        raise JobNotReady(workload_id, state)
    return {"id": workload.id, "result": workload.result}


@router.post("/v1/jobs/{workload_id}/cancel")
async def cancel_job(workload_id: PathId, request: Request) -> dict[str, Any]:
    repo = _workload_repo(request)
    workload = await repo.get(workload_id)
    if workload is None:
        raise WorkloadNotFound(workload_id)
    state = workload.state.value if hasattr(workload.state, "value") else workload.state

    if state in _CANCEL_IDEMPOTENT_STATES:
        return _serialize_workload(workload)

    if state in (WorkloadState.QUEUED.value,):
        updated = await repo.update_state(workload_id, WorkloadState.CANCELLED)
        if updated is None:
            raise WorkloadNotFound(workload_id)
        return _serialize_workload(updated)

    if state == WorkloadState.RUNNING.value:
        runpod_job_id = workload.runpod_job_id
        if runpod_job_id is None:
            updated = await repo.update_state(workload_id, WorkloadState.CANCELLED)
            if updated is None:
                raise WorkloadNotFound(workload_id)
            return _serialize_workload(updated)

        endpoint_id = workload.provider_id
        api_key = (
            request.app.state.runpod_api_key if hasattr(request.app.state, "runpod_api_key") else ""
        )
        client = QueueClient(api_key=api_key)
        try:
            await client.cancel(endpoint_id, runpod_job_id)
        except (
            Exception
        ) as exc:  # reason: any cancel failure maps to retryable RateLimited for the client
            raise RateLimited(retry_after_s=3) from exc

        updated = await repo.update_state(workload_id, WorkloadState.CANCELLED)
        if updated is None:
            raise WorkloadNotFound(workload_id)
        return _serialize_workload(updated)

    return _serialize_workload(workload)


__all__ = ["router"]
