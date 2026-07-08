"""FastAPI route handlers for the Inference surface.

POST /v1/inference handles synchronous inference requests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse

from pitwall.api.exceptions import (
    CapabilityDisabled,
    CapabilityNotFound,
    ChangeSetTooBroad,
    IdempotencyMismatch,
    PreSpendPayloadRejected,
    ProviderNotFound,
    ProviderUnavailable,
)
from pitwall.api.schemas.inference import InferenceRequest, InferenceResponse
from pitwall.api.schemas.leases import lease_patch_conflicting_fields
from pitwall.audit.sixteen_check import PreSpendDecision, scan_pre_spend_payload
from pitwall.config import load_settings_from_env
from pitwall.core.inference import (
    record_inference_trace,
    resolve_inference_target,
    run_sync_inference,
)
from pitwall.core.models import Capability, Provider
from pitwall.db.repository import CapabilityRepository, ProviderRepository
from pitwall.resolver import (
    CapabilityDisabledError,
    CapabilityNotFoundError,
    NoHealthyProviderError,
    ProviderNotFoundError,
)
from pitwall.routing.coalescing import (
    AsyncRequestCoalescer,
    build_inference_coalescing_key,
)

router = APIRouter()

_IDEMPOTENCY_REPLAY_SQL = """
    SELECT id, state, input, result
    FROM pitwall.workloads
    WHERE idempotency_key = $1
"""

_INFERENCE_CONTROL_FIELDS = {
    "capability_id",
    "capability",
    "capability_name",
    "provider_id",
    "dry_run",
    "idempotency_key",
}


@dataclass(frozen=True)
class _InferenceExecutionResult:
    workload_id: str
    runpod_result: Any
    headers: dict[str, str]


_INFERENCE_COALESCER = AsyncRequestCoalescer[_InferenceExecutionResult]()


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return pool


def _capability_repo(request: Request) -> CapabilityRepository:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return CapabilityRepository(pool)


def _provider_repo(request: Request) -> ProviderRepository:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return ProviderRepository(pool)


def _json_bytes(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
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
        "workload_id": str(_row_value(row, "id")),
        "state": str(_row_value(row, "state")),
        "input": _row_value(row, "input"),
        "result": _row_value(row, "result"),
    }


def _row_value(row: Any, key: str) -> Any:
    return row[key]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


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

    result = replay["result"]
    if isinstance(result, dict):
        result_payload = result
    elif result is None:
        result_payload = {"status": replay["state"]}
    else:
        result_payload = {"result": result}

    return {
        "workload_id": replay["workload_id"],
        "result": result_payload,
    }


async def _execute_sync_inference(
    pool: Any,
    *,
    capability: Capability,
    provider: Provider,
    capability_params: dict[str, Any],
    idempotency_key: str | None,
    payload_bytes: int,
    eligible_provider_ids: list[str],
) -> _InferenceExecutionResult:
    settings = load_settings_from_env()
    gated = await run_sync_inference(
        pool,
        capability=capability,
        provider=provider,
        capability_params=capability_params,
        settings=settings,
        idempotency_key=idempotency_key,
        input_bytes=payload_bytes,
        eligible_provider_ids=eligible_provider_ids,
    )

    output_bytes = _json_bytes(gated.runpod_result)
    trace_id = await record_inference_trace(
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

    response_headers = {
        "X-Pitwall-Workload-ID": gated.workload_id,
        "X-Pitwall-Capability": capability.name,
    }
    if isinstance(trace_id, str):
        response_headers["X-Pitwall-Trace"] = trace_id

    return _InferenceExecutionResult(
        workload_id=gated.workload_id,
        runpod_result=gated.runpod_result,
        headers=response_headers,
    )


@router.post(
    "/v1/inference",
    response_model=InferenceResponse,
)
async def create_inference(
    body: InferenceRequest,
    request: Request,
    idempotency_key_header: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ] = None,
    pool: Any = Depends(_pool),
    capability_repo: CapabilityRepository = Depends(_capability_repo),
    provider_repo: ProviderRepository = Depends(_provider_repo),
) -> Response:
    raw_body = await request.json()
    payload_bytes = len(await request.body())
    conflicting_fields = lease_patch_conflicting_fields(raw_body)
    if conflicting_fields:
        raise ChangeSetTooBroad(conflicting_fields)

    idempotency_key = idempotency_key_header or body.idempotency_key
    capability_params = {k: v for k, v in raw_body.items() if k not in _INFERENCE_CONTROL_FIELDS}
    guardrail = scan_pre_spend_payload(capability_params)
    if guardrail.decision == PreSpendDecision.BLOCK:
        raise PreSpendPayloadRejected(
            decision=guardrail.decision.value,
            findings=[finding.to_dict() for finding in guardrail.findings],
        )
    if isinstance(guardrail.redacted_payload, dict):
        capability_params = guardrail.redacted_payload
    replay = await _replay_idempotent_inference(
        pool,
        idempotency_key=idempotency_key,
        capability_params=capability_params,
    )
    if replay is not None:
        return JSONResponse(
            content=replay,
            headers={"X-Pitwall-Workload-ID": replay["workload_id"]},
        )

    try:
        resolution = await resolve_inference_target(
            capability_id=body.capability_id,
            capability_repo=capability_repo,
            provider_repo=provider_repo,
            provider_id=body.provider_id,
            payload_bytes=payload_bytes,
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

    if body.dry_run:
        workload_id = f"dry_run_inference_{capability.id[:8]}"
        return JSONResponse(
            content={
                "workload_id": workload_id,
                "result": {
                    "dry_run": True,
                    "capability_id": capability.id,
                    "capability_name": capability.name,
                    "selected_provider_id": provider.id,
                    "provider_type": provider.provider_type.value,
                    "eligible_provider_ids": [
                        eligible.id for eligible in resolution.eligible_providers
                    ],
                },
            },
            headers={
                "X-Pitwall-Workload-ID": workload_id,
                "X-Pitwall-Capability": capability.name,
            },
        )

    eligible_provider_ids = [eligible.id for eligible in resolution.eligible_providers]
    coalescing_key = build_inference_coalescing_key(
        idempotency_key=idempotency_key,
        capability_id=capability.id,
        provider_id=provider.id,
        capability_params=capability_params,
    )
    execution = await _INFERENCE_COALESCER.run(
        coalescing_key,
        lambda: _execute_sync_inference(
            pool,
            capability=capability,
            provider=provider,
            capability_params=capability_params,
            idempotency_key=idempotency_key,
            payload_bytes=payload_bytes,
            eligible_provider_ids=eligible_provider_ids,
        ),
    )

    return JSONResponse(
        content={"workload_id": execution.workload_id, "result": execution.runpod_result},
        headers=execution.headers,
    )


__all__ = ["router"]
