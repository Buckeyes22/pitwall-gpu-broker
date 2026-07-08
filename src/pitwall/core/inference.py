"""Shared inference orchestration service.

Owns every business-logic internal the MCP layer must not import directly
: routing-request construction, the cost budget/sync gates, and the
RunPod serverless-LB and queue clients. Both the REST ``/v1/inference`` route
and the MCP inference tools delegate here.

Imported by full path (``from pitwall.core import inference``); deliberately
NOT re-exported from ``pitwall.core.__init__`` so the package initializer stays
free of the heavier cost/routing/runpod dependency graph and no import cycle
forms with ``pitwall.cost.sync_gate`` (which imports core leaf modules).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from json import JSONDecodeError
from typing import Any, cast

import asyncpg
import httpx
from pydantic import ValidationError

from pitwall.api.exceptions import ProviderNotFound
from pitwall.config import PitwallSettings
from pitwall.core.enums import WorkloadState
from pitwall.core.jobs import transition_workload
from pitwall.core.models import Capability, Provider, Workload
from pitwall.cost.budget_gate import BudgetGate
from pitwall.cost.estimator import quote_cost
from pitwall.cost.sync_gate import gate_sync_inference
from pitwall.db.repository import (
    CapabilityRepository,
    ProviderRepository,
    WorkloadRepository,
)
from pitwall.observability.langfuse import emit_inference_trace
from pitwall.resolver import Stage12Resolution, resolve_capability
from pitwall.routing import RoutingRequest
from pitwall.runpod_client.queue import QueueClient
from pitwall.runpod_client.serverless_lb import ServerlessLBClient
from pitwall.webhook_dispatcher.security import resolve_webhook_target

log = logging.getLogger("pitwall.core.inference")

_WORKLOAD_TYPE_ASYNC_JOB = "async_job"

_UPDATE_LANGFUSE_TRACE_SQL = """
    UPDATE pitwall.workloads SET langfuse_trace_id = $2 WHERE id = $1
"""

_UPDATE_RUNPOD_JOB_ID_SQL = """
    UPDATE pitwall.workloads SET runpod_job_id = $2 WHERE id = $1
"""

_UPDATE_ASYNC_WORKLOAD_INPUT_SQL = """
    UPDATE pitwall.workloads SET input = $2::jsonb WHERE id = $1
"""


@dataclass(frozen=True)
class GatedSyncInference:
    """Outcome of a gated synchronous inference call."""

    workload_id: str
    runpod_result: Any
    execution_ms: int


@dataclass(frozen=True)
class CancelOutcome:
    """Result of an async-job cancel attempt.

    ``cancelled`` is True only when ``workload`` is present; the ``(None, True)``
    state is illegal and rejected in ``__post_init__``.
    """

    workload: Workload | None
    cancelled: bool

    def __post_init__(self) -> None:
        if self.cancelled and self.workload is None:
            raise ValueError("cancelled=True requires a non-None workload")


async def resolve_inference_target(
    *,
    capability_id: str,
    capability_repo: CapabilityRepository,
    provider_repo: ProviderRepository,
    provider_id: str | None,
    payload_bytes: int,
    provider_limit: int = 10,
) -> Stage12Resolution:
    """Build the RoutingRequest and resolve a capability to one provider.

    Raises the resolver's own errors unchanged (CapabilityNotFoundError /
    CapabilityDisabledError / NoHealthyProviderError / ProviderNotFoundError);
    callers translate these to their transport error vocabulary.
    """
    routing_request = RoutingRequest(
        capability_name=capability_id,
        capability_id=capability_id,
        payload_bytes=payload_bytes,
    )
    return await resolve_capability(
        capability_id,
        capability_repo=capability_repo,
        provider_repo=provider_repo,
        provider_id=provider_id,
        request=routing_request,
        provider_limit=provider_limit,
    )


async def _run_serverless_lb_inference(
    provider: Provider,
    capability_params: dict[str, Any],
    api_key: str | None,
) -> dict[str, Any]:
    """Execute inference against a serverless_lb provider."""
    endpoint_id = provider.runpod_endpoint_id
    if not endpoint_id:
        raise ProviderNotFound(provider.id)

    base_url = f"https://{endpoint_id}.api.runpod.ai"
    # Single attempt: the LB surface holds requests while no worker is ready,
    # so each retry burns the full read-timeout budget and a sync caller would
    # wait attempts x 330s before seeing the provider failure.
    client = ServerlessLBClient(lb_base_url=base_url, api_key=api_key, retry_attempts=1)
    try:
        return await client.embed(
            texts=capability_params.get("texts", []),
            return_dense=capability_params.get("return_dense", True),
            return_sparse=capability_params.get("return_sparse", True),
            return_colbert=capability_params.get("return_colbert", False),
        )
    finally:
        await client.aclose()


async def run_sync_inference(
    pool: asyncpg.Pool,
    *,
    capability: Capability,
    provider: Provider,
    capability_params: dict[str, Any],
    settings: PitwallSettings,
    idempotency_key: str | None,
    input_bytes: int,
    eligible_provider_ids: list[str],
) -> GatedSyncInference:
    """Construct the budget gate, admit + execute the sync RunPod call, time it.

    Raises BudgetRejected when cost admission fails (callers map to HTTP 402 /
    MCP error).
    """
    budget_gate = BudgetGate(
        pool,
        monthly_budget_usd=settings.pitwall_monthly_budget_usd,
        per_request_max_usd=settings.pitwall_per_request_max_usd,
    )

    async def runpod_caller() -> Any:
        return await _run_serverless_lb_inference(
            provider, capability_params, settings.runpod_api_key
        )

    started_at = time.perf_counter()
    admitted = await gate_sync_inference(
        capability=capability,
        provider_id=provider.id,
        provider_cost=provider.config,
        payload=capability_params,
        budget_gate=budget_gate,
        runpod_caller=runpod_caller,
        idempotency_key=idempotency_key,
        input_bytes=input_bytes,
        fallback_chain=eligible_provider_ids,
    )
    execution_ms = int((time.perf_counter() - started_at) * 1000)
    return GatedSyncInference(
        workload_id=admitted.workload_id,
        runpod_result=admitted.runpod_result,
        execution_ms=execution_ms,
    )


async def record_inference_trace(
    pool: asyncpg.Pool,
    *,
    workload_id: str,
    capability_name: str,
    provider_id: str,
    provider_type: str,
    runpod_endpoint_id: str | None,
    cost_estimate_usd: float | None,
    input_bytes: int,
    output_bytes: int,
    execution_ms: int,
    status: str,
    error: BaseException | None = None,
) -> str | None:
    """Emit a Langfuse inference trace and persist its id on the workload row.

    Callers pass already-computed byte counts so each transport's existing
    byte-counting semantics are preserved.
    """
    trace_id = emit_inference_trace(
        workload_id=workload_id,
        capability_name=capability_name,
        provider_id=provider_id,
        provider_type=provider_type,
        runpod_endpoint_id=runpod_endpoint_id,
        cost_estimate_usd=cost_estimate_usd,
        input_bytes=input_bytes,
        output_bytes=output_bytes,
        execution_ms=execution_ms,
        status=status,
        error=error,
    )
    if trace_id:
        async with pool.acquire() as conn:
            await conn.execute(_UPDATE_LANGFUSE_TRACE_SQL, workload_id, trace_id)
    return trace_id


async def create_and_dispatch_job(
    pool: asyncpg.Pool,
    *,
    capability: Capability,
    provider: Provider,
    capability_params: dict[str, Any],
    idempotency_key: str | None,
    webhook_url: str | None,
    settings: PitwallSettings,
) -> Workload:
    """Admit a QUEUED async-job workload, dispatch it to RunPod, persist the
    runpod job id, and return the admitted Workload.

    Validates the provider endpoint *before* inserting so a misconfigured
    provider cannot leave an orphaned QUEUED row. Raises ProviderNotFound when
    the provider has no runpod_endpoint_id.
    """
    endpoint_id = provider.runpod_endpoint_id
    if not endpoint_id:
        raise ProviderNotFound(provider.id)

    normalized_webhook_url: str | None = None
    if webhook_url is not None:
        normalized_webhook_url = (await resolve_webhook_target(webhook_url)).url

    budget_gate = BudgetGate(
        pool,
        monthly_budget_usd=settings.pitwall_monthly_budget_usd,
        per_request_max_usd=settings.pitwall_per_request_max_usd,
    )
    cost_quote = quote_cost(
        capability=capability,
        provider_cost=provider.config,
        payload=capability_params,
    )
    submitted_at = dt.datetime.now(dt.UTC)
    admitted = await budget_gate.try_launch_admission(
        capability_id=capability.id,
        provider_id=provider.id,
        estimate_usd=cost_quote,
        workload_type=_WORKLOAD_TYPE_ASYNC_JOB,
        submitted_at=submitted_at,
        idempotency_key=idempotency_key,
    )

    workload_repo = WorkloadRepository(pool)
    if not admitted.is_new:
        return await _load_admitted_workload(workload_repo, admitted.workload_id)

    workload_id = admitted.workload_id
    await _update_async_workload_input(
        pool,
        workload_id=workload_id,
        capability_params=capability_params,
    )
    queue_client = QueueClient(api_key=settings.runpod_api_key or "")
    try:
        queue_job = await queue_client.run(
            endpoint_id,
            input=capability_params,
            webhook=normalized_webhook_url,
        )
    except (httpx.HTTPError, RuntimeError, JSONDecodeError, ValidationError) as exc:
        await _mark_async_dispatch_failed(pool, workload_id=workload_id, error=exc)
        raise

    async with pool.acquire() as conn:
        await conn.execute(_UPDATE_RUNPOD_JOB_ID_SQL, workload_id, queue_job.id)

    return await _load_admitted_workload(workload_repo, workload_id)


async def _update_async_workload_input(
    pool: asyncpg.Pool,
    *,
    workload_id: str,
    capability_params: dict[str, Any],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_UPDATE_ASYNC_WORKLOAD_INPUT_SQL, workload_id, capability_params)


async def _mark_async_dispatch_failed(
    pool: asyncpg.Pool,
    *,
    workload_id: str,
    error: Exception,
) -> None:
    async with pool.acquire() as conn, conn.transaction():
        transitioned = await transition_workload(
            cast(asyncpg.Connection, conn),
            workload_id=workload_id,
            from_states={WorkloadState.QUEUED.value},
            to_state=WorkloadState.FAILED.value,
            patch={
                "completed_at": dt.datetime.now(dt.UTC),
                "cost_actual_usd": Decimal("0"),
                "error": _error_payload(error),
            },
        )
    if not transitioned:
        log.warning(
            "async dispatch failed but workload %s was no longer queued; "
            "cost reservation may require reconciliation",
            workload_id,
        )


async def _load_admitted_workload(
    workload_repo: WorkloadRepository,
    workload_id: str,
) -> Workload:
    workload = await workload_repo.get(workload_id)
    if workload is None:
        raise RuntimeError(f"admitted workload {workload_id!r} was not found")
    return workload


def _error_payload(exc: Exception) -> dict[str, Any]:
    return {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }


async def cancel_job(
    pool: asyncpg.Pool,
    *,
    workload_id: str,
    settings: PitwallSettings,
) -> CancelOutcome:
    """Cancel a QUEUED|RUNNING async job.

    Returns a :class:`CancelOutcome`:
      - ``CancelOutcome(None, False)``      the workload does not exist
      - ``CancelOutcome(workload, False)``  the state transition did not apply
      - ``CancelOutcome(refreshed, True)``  cancelled (best-effort RunPod cancel)
    """
    workload_repo = WorkloadRepository(pool)
    workload = await workload_repo.get(workload_id)
    if workload is None:
        return CancelOutcome(None, False)

    async with pool.acquire() as conn:
        transitioned = await transition_workload(
            cast(asyncpg.Connection, conn),
            workload_id=workload_id,
            from_states={WorkloadState.QUEUED.value, WorkloadState.RUNNING.value},
            to_state=WorkloadState.CANCELLED.value,
        )
    if not transitioned:
        return CancelOutcome(workload, False)

    if workload.runpod_job_id and workload.provider_id:
        provider_repo = ProviderRepository(pool)
        provider = await provider_repo.get(workload.provider_id)
        if provider and provider.runpod_endpoint_id:
            queue_client = QueueClient(api_key=settings.runpod_api_key or "")
            try:
                await queue_client.cancel(provider.runpod_endpoint_id, workload.runpod_job_id)
            except (
                Exception
            ):  # reason: best-effort RunPod cancel; workload already CANCELLED locally
                log.warning(
                    "RunPod cancel failed for workload %s (job %s); ledger already "
                    "CANCELLED, the RunPod job may still be running",
                    workload_id,
                    workload.runpod_job_id,
                    exc_info=True,
                )

    refreshed = await workload_repo.get(workload_id)
    return CancelOutcome(refreshed if refreshed is not None else workload, True)
