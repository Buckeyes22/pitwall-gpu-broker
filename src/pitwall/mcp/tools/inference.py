"""Inference tools — sync and async inference submission for the MCP surface.

These tools expose the same inference operations as the REST API endpoints:
- POST /v1/inference        -> pitwall_submit_inference
- POST /v1/jobs             -> pitwall_submit_job   (MCP-only; no REST equivalent)
- GET  /v1/jobs/{id}/status -> pitwall_get_job_status
- GET  /v1/jobs/{id}/result -> pitwall_get_job_result
- POST /v1/jobs/{id}/cancel -> pitwall_cancel_job

All business-logic orchestration (routing, the budget/sync cost gates, and the
RunPod clients) lives in ``pitwall.core.inference``. These handlers are thin
wrappers that delegate there and shape the MCP response: idempotency replay,
dry-run, and ``normalize_workload_output``.
"""

from __future__ import annotations

import json
from typing import Any

from pitwall.api.exceptions import (
    CapabilityDisabled,
    CapabilityNotFound,
    IdempotencyMismatch,
    ProviderNotFound,
    ProviderUnavailable,
    WorkloadNotFound,
)
from pitwall.config import load_settings_from_env
from pitwall.core import inference as inference_service
from pitwall.db import get_pool
from pitwall.db.repository import (
    CapabilityRepository,
    ProviderRepository,
    WorkloadRepository,
)
from pitwall.mcp.tools.output import normalize_workload_output
from pitwall.resolver import (
    CapabilityDisabledError,
    CapabilityNotFoundError,
    NoHealthyProviderError,
    ProviderNotFoundError,
)

_CONTROL_FIELDS = {
    "capability_id",
    "capability",
    "capability_name",
    "provider_id",
    "dry_run",
    "idempotency_key",
}

_IDEMPOTENCY_REPLAY_SQL = """
    SELECT id, state, input, result
    FROM pitwall.workloads
    WHERE idempotency_key = $1
"""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


async def _lookup_idempotent_workload(
    pool: Any,
    idempotency_key: str,
) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_IDEMPOTENCY_REPLAY_SQL, idempotency_key)
    if row is None:
        return None
    return {
        "workload_id": str(row["id"]),
        "state": str(row["state"]),
        "input": row["input"],
        "result": row["result"],
    }


async def _replay_idempotent_inference(
    pool: Any,
    *,
    idempotency_key: str | None,
    capability_params: dict[str, Any],
) -> dict[str, Any] | None:
    if idempotency_key is None:
        return None

    replay = await _lookup_idempotent_workload(pool, idempotency_key)
    if replay is None:
        return None

    original_input = replay["input"]
    if original_input is not None and _canonical_json(original_input) != _canonical_json(
        capability_params
    ):
        raise IdempotencyMismatch(str(replay["workload_id"]))

    workload_repo = WorkloadRepository(pool)
    workload = await workload_repo.get(replay["workload_id"])
    if workload is not None:
        return normalize_workload_output(workload)

    result = replay["result"]
    if isinstance(result, dict):
        result_payload = result
    elif result is None:
        result_payload = {"status": replay["state"]}
    else:
        result_payload = {"result": result}

    return {
        "workload_id": replay["workload_id"],
        "cost": {"estimate_usd": None, "actual_usd": None},
        "provider_id": None,
        "state": replay["state"],
        "result": result_payload,
        "trace_id": None,
    }


async def pitwall_submit_inference(
    capability_id: str,
    payload: dict[str, Any] | None = None,
    provider_id: str | None = None,
    dry_run: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Submit a synchronous inference request to a capability.

    Mirrors POST /v1/inference. Capability-specific parameters (e.g.
    ``{"texts": [...]}`` for embedding) go in ``payload``. An explicit dict —
    not ``**kwargs`` — because FastMCP schematizes ``**kwargs`` as a literal
    required field, making the tool uncallable over the MCP wire protocol.
    """
    pool = await get_pool()
    capability_repo = CapabilityRepository(pool)
    provider_repo = ProviderRepository(pool)

    capability_params = {k: v for k, v in (payload or {}).items() if k not in _CONTROL_FIELDS}

    replay = await _replay_idempotent_inference(
        pool,
        idempotency_key=idempotency_key,
        capability_params=capability_params,
    )
    if replay is not None:
        return replay

    try:
        resolution = await inference_service.resolve_inference_target(
            capability_id=capability_id,
            capability_repo=capability_repo,
            provider_repo=provider_repo,
            provider_id=provider_id,
            payload_bytes=0,
        )
    except CapabilityNotFoundError as exc:
        raise CapabilityNotFound(exc.capability_name) from exc
    except CapabilityDisabledError as exc:
        raise CapabilityDisabled(exc.capability_name) from exc
    except NoHealthyProviderError as exc:
        raise ProviderUnavailable(exc.capability_name) from exc
    except ProviderNotFoundError as exc:
        raise ProviderNotFound(exc.provider_id) from exc

    capability = resolution.capability
    provider = resolution.provider

    settings = load_settings_from_env()
    payload_bytes = len(_canonical_json(capability_params))

    if dry_run:
        workload_id = f"dry_run_inference_{capability.id[:8]}"
        return {
            "workload_id": workload_id,
            "cost": {"estimate_usd": None, "actual_usd": None},
            "provider_id": provider.id,
            "state": "completed",
            "result": {
                "dry_run": True,
                "capability_id": capability.id,
                "capability_name": capability.name,
                "selected_provider_id": provider.id,
                "provider_type": provider.provider_type.value,
                "eligible_provider_ids": [p.id for p in resolution.eligible_providers],
            },
            "trace_id": None,
        }

    gated = await inference_service.run_sync_inference(
        pool,
        capability=capability,
        provider=provider,
        capability_params=capability_params,
        settings=settings,
        idempotency_key=idempotency_key,
        input_bytes=payload_bytes,
        eligible_provider_ids=[p.id for p in resolution.eligible_providers],
    )

    output_bytes = len(_canonical_json(gated.runpod_result))
    await inference_service.record_inference_trace(
        pool,
        workload_id=gated.workload_id,
        capability_name=capability.name,
        provider_id=provider.id,
        provider_type=provider.provider_type.value,
        runpod_endpoint_id=provider.runpod_endpoint_id,
        cost_estimate_usd=None,
        input_bytes=payload_bytes,
        output_bytes=output_bytes,
        execution_ms=gated.execution_ms,
        status="success",
    )

    workload_repo = WorkloadRepository(pool)
    workload = await workload_repo.get(gated.workload_id)
    if workload is not None:
        return normalize_workload_output(workload)

    return {
        "workload_id": gated.workload_id,
        "cost": {"estimate_usd": None, "actual_usd": None},
        "provider_id": provider.id,
        "state": "completed",
        "result": gated.runpod_result,
        "trace_id": None,
    }


async def pitwall_submit_job(
    capability_id: str,
    input: dict[str, Any],
    provider_id: str | None = None,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    """Submit an asynchronous job to a capability.

    Mirrors POST /v1/jobs (MCP-only; there is no REST equivalent).
    Capability-specific parameters go in ``input``.
    """
    pool = await get_pool()
    capability_repo = CapabilityRepository(pool)
    provider_repo = ProviderRepository(pool)

    capability_params: dict[str, Any] = {}
    if input:
        capability_params["input"] = input

    try:
        resolution = await inference_service.resolve_inference_target(
            capability_id=capability_id,
            capability_repo=capability_repo,
            provider_repo=provider_repo,
            provider_id=provider_id,
            payload_bytes=len(_canonical_json(capability_params)),
        )
    except CapabilityNotFoundError as exc:
        raise CapabilityNotFound(exc.capability_name) from exc
    except CapabilityDisabledError as exc:
        raise CapabilityDisabled(exc.capability_name) from exc
    except NoHealthyProviderError as exc:
        raise ProviderUnavailable(exc.capability_name) from exc
    except ProviderNotFoundError as exc:
        raise ProviderNotFound(exc.provider_id) from exc

    capability = resolution.capability
    provider = resolution.provider

    replay = await _replay_idempotent_inference(
        pool,
        idempotency_key=idempotency_key,
        capability_params=capability_params,
    )
    if replay is not None:
        return replay

    if dry_run:
        workload_id = f"dry_run_job_{capability.id[:8]}"
        return {
            "workload_id": workload_id,
            "cost": {"estimate_usd": None, "actual_usd": None},
            "provider_id": provider.id,
            "state": "queued",
            "result": {
                "dry_run": True,
                "capability_id": capability.id,
                "capability_name": capability.name,
                "selected_provider_id": provider.id,
                "provider_type": provider.provider_type.value,
                "eligible_provider_ids": [p.id for p in resolution.eligible_providers],
            },
            "trace_id": None,
        }

    workload = await inference_service.create_and_dispatch_job(
        pool,
        capability=capability,
        provider=provider,
        capability_params=capability_params,
        idempotency_key=idempotency_key,
        webhook_url=webhook_url,
        settings=load_settings_from_env(),
    )
    return normalize_workload_output(workload)


async def pitwall_get_job_status(
    workload_id: str,
) -> dict[str, Any]:
    """Return the current state of an async job by workload ID.

    Mirrors GET /v1/jobs/{id}/status.
    """
    pool = await get_pool()
    workload_repo = WorkloadRepository(pool)
    workload = await workload_repo.get(workload_id)
    if workload is None:
        raise WorkloadNotFound(workload_id)

    return normalize_workload_output(workload)


async def pitwall_get_job_result(
    workload_id: str,
) -> dict[str, Any]:
    """Return the completed result of an async job by workload ID.

    Mirrors GET /v1/jobs/{id}/result.
    """
    pool = await get_pool()
    workload_repo = WorkloadRepository(pool)
    workload = await workload_repo.get(workload_id)
    if workload is None:
        raise WorkloadNotFound(workload_id)

    return normalize_workload_output(workload)


async def pitwall_cancel_job(
    workload_id: str,
) -> dict[str, Any]:
    """Cancel a pending or running async job by workload ID.

    Mirrors POST /v1/jobs/{id}/cancel.
    """
    pool = await get_pool()
    outcome = await inference_service.cancel_job(
        pool,
        workload_id=workload_id,
        settings=load_settings_from_env(),
    )
    if outcome.workload is None:
        raise WorkloadNotFound(workload_id)

    output = normalize_workload_output(outcome.workload)
    output["cancelled"] = outcome.cancelled
    return output
