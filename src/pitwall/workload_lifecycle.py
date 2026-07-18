"""Workload lifecycle management for pass-through requests.

Inserts and updates workload rows to track the lifecycle of OpenAI-compatible
pass-through requests through Pitwall. The workload row is created as 'queued'
when a request arrives, transitioned to 'running' once the upstream is called,
and completed or failed based on the outcome. The external OpenAI response is
never modified.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

from pitwall.core.enums import WorkloadState
from pitwall.core.ids import ulid_new
from pitwall.core.models import Workload
from pitwall.db.repository import WorkloadRepository

log = logging.getLogger("pitwall.workload_lifecycle")

try:
    from arq import ArqRedis
    from arq.connections import RedisSettings, create_pool

    _ARQ_AVAILABLE = True
except ImportError:  # pragma: no cover
    ArqRedis = None  # type: ignore[assignment, misc]  # reason: arq optional; None sentinel when uninstalled
    RedisSettings = None  # type: ignore[assignment, misc]  # reason: arq optional; None sentinel when uninstalled
    create_pool = None  # type: ignore[assignment]  # reason: arq optional; None sentinel when uninstalled
    _ARQ_AVAILABLE = False

_WORKLOAD_TYPE_OPENAI_PASSTHROUGH = "openai_passthrough"


def generate_workload_id() -> str:
    return f"wkl_{ulid_new()}"


async def insert_passthrough_workload(
    repo: WorkloadRepository,
    *,
    workload_id: str,
    capability_id: str,
    provider_id: str,
    idempotency_key: str | None = None,
    input_data: dict[str, Any] | None = None,
    input_bytes: int | None = None,
) -> Workload:
    now = dt.datetime.now(dt.UTC)
    workload = Workload(
        id=workload_id,
        capability_id=capability_id,
        provider_id=provider_id,
        type=_WORKLOAD_TYPE_OPENAI_PASSTHROUGH,
        state=WorkloadState.QUEUED,
        idempotency_key=idempotency_key,
        input=input_data,
        input_bytes=input_bytes,
        submitted_at=now,
    )
    return await repo.insert(workload)


async def transition_to_running(
    repo: WorkloadRepository,
    workload_id: str,
    *,
    provider_id: str | None = None,
    fallback_chain: list[str] | None = None,
) -> Workload | None:
    now = dt.datetime.now(dt.UTC)
    patch: dict[str, Any] = {"started_at": now}
    if fallback_chain is not None:
        patch["fallback_chain"] = fallback_chain if fallback_chain else None
    return await repo.guarded_transition(
        workload_id,
        from_states={WorkloadState.QUEUED},
        to_state=WorkloadState.RUNNING,
        patch=patch,
    )


async def transition_to_completed(
    repo: WorkloadRepository,
    workload_id: str,
    *,
    execution_ms: int | None = None,
    output_bytes: int | None = None,
    result: dict[str, Any] | None = None,
    fallback_chain: list[str] | None = None,
    langfuse_trace_id: str | None = None,
) -> Workload | None:
    now = dt.datetime.now(dt.UTC)
    patch: dict[str, Any] = {
        "completed_at": now,
        "execution_ms": execution_ms,
        "output_bytes": output_bytes,
        "result": result,
        "fallback_chain": fallback_chain if fallback_chain else None,
        "langfuse_trace_id": langfuse_trace_id,
    }
    return await repo.guarded_transition(
        workload_id,
        from_states={WorkloadState.RUNNING},
        to_state=WorkloadState.COMPLETED,
        patch=patch,
    )


async def transition_to_failed(
    repo: WorkloadRepository,
    workload_id: str,
    *,
    execution_ms: int | None = None,
    error: dict[str, Any] | None = None,
    fallback_chain: list[str] | None = None,
    langfuse_trace_id: str | None = None,
) -> Workload | None:
    now = dt.datetime.now(dt.UTC)
    patch: dict[str, Any] = {
        "completed_at": now,
        "execution_ms": execution_ms,
        "error": error,
        "fallback_chain": fallback_chain if fallback_chain else None,
        "langfuse_trace_id": langfuse_trace_id,
    }
    return await repo.guarded_transition(
        workload_id,
        from_states={WorkloadState.RUNNING},
        to_state=WorkloadState.FAILED,
        patch=patch,
    )


async def enqueue_submit_runpod_job(workload_id: str) -> None:
    """Enqueue a submit_runpod_job Arq job for the given workload.

    This function creates an ArqRedis connection and enqueues the job.
    It should be called only after the workload row has been committed to the database.

    Args:
        workload_id: The ID of the workload to enqueue for processing.
    """
    if not _ARQ_AVAILABLE:
        log.warning("arq is not available; cannot enqueue submit_runpod_job for %s", workload_id)
        return

    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        log.warning("REDIS_URL is not set; cannot enqueue submit_runpod_job for %s", workload_id)
        return

    redis_settings = RedisSettings.from_dsn(redis_url)
    arq_redis = await create_pool(redis_settings)
    try:
        await arq_redis.enqueue_job("submit_runpod_job", workload_id)
    finally:
        await arq_redis.aclose()


__all__ = [
    "enqueue_submit_runpod_job",
    "generate_workload_id",
    "insert_passthrough_workload",
    "transition_to_completed",
    "transition_to_failed",
    "transition_to_running",
]
