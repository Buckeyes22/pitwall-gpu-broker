"""Synchronous inference cost gate.

Estimates and admits before any synchronous RunPod call:

    resolve capability → pick provider → estimate cost → budget gate → RunPod

This module is the single entry point that wires together the three
components that must run *before* RunPod sees a request:

1. **CostEstimator** — computes a USD estimate from the capability's
   ``cost_mode`` and the provider's cost profile.
2. **BudgetGate.try_launch** — admits the workload under the advisory
   lock and persists a ``queued`` row.  Raises ``BudgetRejected`` when
   the monthly or per-request cap would be exceeded.
3. **RunPod sync client** — the actual ``/runsync``, LB custom-path, or
   OpenAI-compatible call.  Only reached after admission succeeds.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from pitwall.core.idempotency import reserve_idempotency_key
from pitwall.core.models import Capability
from pitwall.cost.budget_gate import BudgetGate, BudgetRejected
from pitwall.cost.estimator import CostQuote, EstimatePayload, ProviderCost, quote_cost

log = logging.getLogger("pitwall.cost.sync_gate")

_MARK_WORKLOAD_RUNNING_SQL = """
    UPDATE pitwall.workloads
    SET state = 'running',
        started_at = $2,
        queue_ms = GREATEST(
            0,
            FLOOR(EXTRACT(EPOCH FROM ($2 - submitted_at)) * 1000)::integer
        ),
        input_bytes = $3,
        input = $4::jsonb,
        fallback_chain = $5::text[]
    WHERE id = $1
"""

_MARK_WORKLOAD_TERMINAL_SQL = """
    UPDATE pitwall.workloads
    SET state = $2,
        completed_at = $3,
        execution_ms = $4,
        output_bytes = $5,
        result = $6::jsonb,
        runpod_job_id = COALESCE($7::text, runpod_job_id),
        error = $8::jsonb
    WHERE id = $1
"""

_MARK_WORKLOAD_ACTIVE_AFTER_CALL_SQL = """
    UPDATE pitwall.workloads
    SET state = $2,
        runpod_job_id = COALESCE($3::text, runpod_job_id),
        output_bytes = $4,
        result = $5::jsonb
    WHERE id = $1
"""

_MARK_WORKLOAD_FAILED_SQL = """
    UPDATE pitwall.workloads
    SET state = 'failed',
        completed_at = $2,
        execution_ms = $3,
        error = $4::jsonb
    WHERE id = $1
"""

_UPDATE_WORKLOAD_FALLBACK_CHAIN_SQL = """
    UPDATE pitwall.workloads
    SET fallback_chain = $2::text[]
    WHERE id = $1
"""

_LOAD_WORKLOAD_REPLAY_SQL = """
    SELECT state, result
    FROM pitwall.workloads
    WHERE id = $1
"""


@dataclass(frozen=True)
class SyncInferenceResult:
    """Outcome of a gated synchronous inference call."""

    workload_id: str
    runpod_result: Any


@runtime_checkable
class RunPodSyncCaller(Protocol):
    """Callable that performs the actual RunPod request after admission."""

    async def __call__(self) -> Any: ...


class SyncInferenceRejected(RuntimeError):
    """Raised when sync inference is rejected before reaching RunPod."""

    def __init__(self, reason: str, budget_error: BudgetRejected) -> None:
        super().__init__(reason)
        self.budget_error = budget_error


def _idempotency_body_hash(payload: EstimatePayload) -> str:
    """Compute canonical SHA-256 hash of the request body for idempotency verification.

    Uses the same canonical JSON format as ``reserve_idempotency_key``.
    """
    if payload is None:
        return hashlib.sha256(b"null").hexdigest()
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def gate_sync_inference(
    *,
    capability: Capability,
    provider_id: str,
    provider_cost: ProviderCost,
    payload: EstimatePayload,
    budget_gate: BudgetGate,
    runpod_caller: RunPodSyncCaller | Callable[[], Awaitable[Any]],
    idempotency_key: str | None = None,
    submitted_at: dt.datetime | None = None,
    input_bytes: int | None = None,
    fallback_chain: list[str] | None = None,
) -> SyncInferenceResult:
    """Estimate, admit, and execute a synchronous inference call.

    Returns a :class:`SyncInferenceResult` containing the admitted
    ``workload_id`` and the raw RunPod response.

    Raises :class:`BudgetRejected` when cost admission fails — the caller
    should translate this to HTTP 402.
    """
    cost_quote = _quote(capability, provider_cost, payload)
    estimate_usd = cost_quote.upper_bound()

    launch_kwargs: dict[str, Any] = {
        "capability_id": capability.id,
        "provider_id": provider_id,
        "estimate_usd": cost_quote,
        "workload_type": "inference",
    }
    if idempotency_key is not None:
        body_hash = _idempotency_body_hash(payload)
        async with budget_gate.pool.acquire() as conn, conn.transaction():
            reservation = await reserve_idempotency_key(
                conn,
                key=idempotency_key,
                body_hash=body_hash,
                workload_id=f"wkl_pending_{idempotency_key[:16]}",
            )
        if not reservation.is_new:
            log.info(
                "idempotency replay: workload_id=%s",
                reservation.workload_id,
            )
        launch_kwargs["idempotency_key"] = idempotency_key
    if submitted_at is not None:
        launch_kwargs["submitted_at"] = submitted_at

    admission = await budget_gate.try_launch_admission(**launch_kwargs)
    workload_id = admission.workload_id
    log.info(
        "sync inference admitted: workload_id=%s estimate_usd=%.6f",
        workload_id,
        estimate_usd,
    )
    if not admission.is_new:
        result = await _load_workload_replay_result(
            budget_gate.pool,
            workload_id=workload_id,
        )
        log.info("sync inference idempotency replay: workload_id=%s", workload_id)
        return SyncInferenceResult(workload_id=workload_id, runpod_result=result)

    started_at = _utc_now()
    input_payload = _json_object(payload, wrapper_key="payload")
    resolved_input_bytes = input_bytes if input_bytes is not None else _json_bytes(input_payload)
    await _mark_workload_running(
        budget_gate.pool,
        workload_id=workload_id,
        started_at=started_at,
        input_bytes=resolved_input_bytes,
        input_payload=input_payload,
        fallback_chain=fallback_chain or [provider_id],
    )

    try:
        result = await runpod_caller()
    except (
        Exception
    ) as exc:  # reason: record terminal ledger state for any provider failure before re-raise
        # Record the terminal ledger state, then re-raise so API error handling remains unchanged.
        completed_at = _utc_now()
        await _mark_workload_failed(
            budget_gate.pool,
            workload_id=workload_id,
            completed_at=completed_at,
            execution_ms=_elapsed_ms(started_at, completed_at),
            error=_error_payload(exc),
        )
        raise

    completed_at = _utc_now()
    result_payload = _json_object(result, wrapper_key="result")
    output_bytes = _json_bytes(result_payload)
    runpod_job_id = _extract_runpod_job_id(result_payload)
    active_state = _active_state_from_runpod_result(result_payload)
    if active_state is None:
        terminal_state = _terminal_state_from_runpod_result(result_payload)
        await _mark_workload_terminal(
            budget_gate.pool,
            workload_id=workload_id,
            state=terminal_state,
            completed_at=completed_at,
            execution_ms=_elapsed_ms(started_at, completed_at),
            output_bytes=output_bytes,
            result_payload=result_payload,
            runpod_job_id=runpod_job_id,
            error=None if terminal_state == "completed" else _result_error_payload(result_payload),
        )
    else:
        await _mark_workload_active_after_call(
            budget_gate.pool,
            workload_id=workload_id,
            state=active_state,
            runpod_job_id=runpod_job_id,
            output_bytes=output_bytes,
            result_payload=result_payload,
        )

    return SyncInferenceResult(workload_id=workload_id, runpod_result=result)


def estimate_cost(
    *,
    capability: Capability,
    provider_cost: ProviderCost,
    payload: EstimatePayload,
) -> Decimal:
    """Public convenience wrapper around the estimator lookup."""
    return _quote(capability, provider_cost, payload).estimate()


def _quote(
    capability: Capability,
    provider_cost: ProviderCost,
    payload: EstimatePayload,
) -> CostQuote:
    return quote_cost(capability=capability, provider_cost=provider_cost, payload=payload)


async def _mark_workload_running(
    pool: Any,
    *,
    workload_id: str,
    started_at: dt.datetime,
    input_bytes: int,
    input_payload: dict[str, Any],
    fallback_chain: list[str],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            _MARK_WORKLOAD_RUNNING_SQL,
            workload_id,
            started_at,
            input_bytes,
            input_payload,
            fallback_chain,
        )


async def _load_workload_replay_result(
    pool: Any,
    *,
    workload_id: str,
) -> Any:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_LOAD_WORKLOAD_REPLAY_SQL, workload_id)
    if row is None:
        raise RuntimeError(f"idempotency replay workload {workload_id!r} was not found")
    result = row["result"]
    if isinstance(result, Mapping):
        return dict(result)
    if result is None:
        return {"status": str(row["state"])}
    return {"result": result}


async def _mark_workload_terminal(
    pool: Any,
    *,
    workload_id: str,
    state: str,
    completed_at: dt.datetime,
    execution_ms: int,
    output_bytes: int,
    result_payload: dict[str, Any],
    runpod_job_id: str | None,
    error: dict[str, Any] | None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            _MARK_WORKLOAD_TERMINAL_SQL,
            workload_id,
            state,
            completed_at,
            execution_ms,
            output_bytes,
            result_payload,
            runpod_job_id,
            error,
        )


async def _mark_workload_active_after_call(
    pool: Any,
    *,
    workload_id: str,
    state: str,
    runpod_job_id: str | None,
    output_bytes: int,
    result_payload: dict[str, Any],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            _MARK_WORKLOAD_ACTIVE_AFTER_CALL_SQL,
            workload_id,
            state,
            runpod_job_id,
            output_bytes,
            result_payload,
        )


async def _mark_workload_failed(
    pool: Any,
    *,
    workload_id: str,
    completed_at: dt.datetime,
    execution_ms: int,
    error: dict[str, Any],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            _MARK_WORKLOAD_FAILED_SQL,
            workload_id,
            completed_at,
            execution_ms,
            error,
        )


async def update_workload_fallback_chain(
    pool: Any,
    workload_id: str,
    fallback_chain: list[str],
) -> None:
    """Update the fallback_chain for a workload after provider attempts."""
    async with pool.acquire() as conn:
        await conn.execute(
            _UPDATE_WORKLOAD_FALLBACK_CHAIN_SQL,
            workload_id,
            fallback_chain,
        )


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _elapsed_ms(started_at: dt.datetime, completed_at: dt.datetime) -> int:
    return max(0, int((completed_at - started_at).total_seconds() * 1000))


def _json_bytes(value: object) -> int:
    return len(
        json.dumps(
            _json_safe(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _json_object(value: object, *, wrapper_key: str) -> dict[str, Any]:
    safe = _json_safe(value)
    if isinstance(safe, dict):
        return safe
    return {wrapper_key: safe}


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_safe(model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return repr(value)


def _extract_runpod_job_id(result_payload: Mapping[str, Any]) -> str | None:
    for key in ("id", "job_id", "jobId", "runpod_job_id"):
        value = result_payload.get(key)
        if value is not None:
            return str(value)

    raw = result_payload.get("raw")
    if isinstance(raw, Mapping):
        for key in ("id", "job_id", "jobId", "runpod_job_id"):
            value = raw.get(key)
            if value is not None:
                return str(value)
    return None


def _active_state_from_runpod_result(result_payload: Mapping[str, Any]) -> str | None:
    status = result_payload.get("status")
    if status == "IN_QUEUE":
        return "queued"
    if status == "IN_PROGRESS":
        return "running"
    return None


def _terminal_state_from_runpod_result(result_payload: Mapping[str, Any]) -> str:
    status = result_payload.get("status")
    if status == "FAILED":
        return "failed"
    if status == "CANCELLED":
        return "cancelled"
    if status in {"TIMED_OUT", "TIMEOUT", "TIME_OUT"}:
        return "timed_out"
    return "completed"


def _result_error_payload(result_payload: Mapping[str, Any]) -> dict[str, Any]:
    error = result_payload.get("error")
    if isinstance(error, Mapping):
        return dict(error)
    if error is not None:
        return {"message": str(error)}
    return {"status": str(result_payload.get("status", "unknown"))}


def _error_payload(exc: Exception) -> dict[str, Any]:
    return {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }


__all__ = [
    "RunPodSyncCaller",
    "SyncInferenceRejected",
    "SyncInferenceResult",
    "estimate_cost",
    "gate_sync_inference",
    "update_workload_fallback_chain",
]
