"""FastAPI route handlers for the OpenAI-compatible pass-through surface.

Transparent proxy at ``/v1/openai/{capability}/v1/*`` that forwards bodies
verbatim, rewrites the upstream URL based on capability resolution, adds Pitwall
observability headers, and traverses the provider chain on pre-relay failures.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from pitwall.api.exceptions import CapabilityNotFound, InvalidProxyPath, ProviderUnavailable
from pitwall.api.schemas.params import PathId
from pitwall.config import load_settings_from_env
from pitwall.core.models import Capability, Provider
from pitwall.cost.budget_gate import BudgetGate
from pitwall.cost.estimator import CostQuote, quote_cost
from pitwall.db.repository import CapabilityRepository, ProviderRepository, WorkloadRepository
from pitwall.observability.langfuse import emit_inference_trace
from pitwall.routing.fallback import (
    DEFAULT_OPENAI_FALLBACK_BUDGET_S,
    OpenAIProxyExecutionError,
    OpenAIProxyRequest,
    execute_openai_with_fallback,
)
from pitwall.routing.openai import (
    OpenAIProviderChain,
    resolve_openai_provider_chain,
    validate_openai_proxy_path,
)
from pitwall.workload_lifecycle import (
    transition_to_completed,
    transition_to_failed,
    transition_to_running,
)

log = logging.getLogger("pitwall.api.routes.openai")

router = APIRouter()

_OPENAI_FALLBACK_BUDGET_S = DEFAULT_OPENAI_FALLBACK_BUDGET_S
_WORKLOAD_TYPE_OPENAI_PASSTHROUGH = "openai_passthrough"


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


def _workload_repo(request: Request) -> WorkloadRepository:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    return WorkloadRepository(pool)


def _budget_gate(request: Request) -> BudgetGate:
    settings = load_settings_from_env()
    return BudgetGate(
        _pool(request),
        monthly_budget_usd=settings.pitwall_monthly_budget_usd,
        per_request_max_usd=settings.pitwall_per_request_max_usd,
    )


async def _resolve_provider_chain(
    capability_name: str,
    capability_repo: CapabilityRepository,
    provider_repo: ProviderRepository,
) -> tuple[Capability, OpenAIProviderChain, list[Provider]]:
    capability = await capability_repo.get_by_name(capability_name)
    if capability is None:
        raise CapabilityNotFound(capability_name)

    providers = await provider_repo.list(
        capability_id=capability.id,
        enabled_only=True,
        limit=100,
    )
    chain = resolve_openai_provider_chain(providers)
    if not chain.attempts:
        raise ProviderUnavailable(capability_name)
    return capability, chain, [p for p in chain.providers if isinstance(p, Provider)]


async def _close_upstream_response(
    upstream_response: httpx.Response,
    client: httpx.AsyncClient,
) -> None:
    await upstream_response.aclose()
    await client.aclose()


_SSE_STREAM_ERROR_EVENT = 'data: {"error":"upstream stream failure"}\n\n'


def _is_sse_response(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return "text/event-stream" in content_type


async def _relay_upstream_bytes(
    upstream_response: httpx.Response,
    client: httpx.AsyncClient,
) -> AsyncIterator[bytes]:
    try:
        async for chunk in upstream_response.aiter_bytes():
            yield chunk
    except Exception as exc:  # reason: mid-stream upstream failure: log and end stream cleanly
        log.warning("mid-stream upstream failure: %s", exc)
        if _is_sse_response(upstream_response):
            yield _SSE_STREAM_ERROR_EVENT.encode("utf-8")
    finally:
        await _close_upstream_response(upstream_response, client)


def _path_with_query(path: str, request: Request) -> str:
    query = request.url.query
    if not query:
        return path
    return f"{path}?{query}"


def _content_length(response: httpx.Response) -> int:
    raw_value = response.headers.get("content-length")
    if raw_value is None:
        return 0
    try:
        return int(raw_value)
    except ValueError:
        return 0


def _estimate_payload_from_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"input_bytes": len(body)}
    if isinstance(parsed, Mapping):
        return dict(parsed)
    return {"input": parsed, "input_bytes": len(body)}


def _quote_proxy_cost(
    capability: Capability,
    provider: Provider,
    body: bytes,
) -> CostQuote:
    return quote_cost(
        capability=capability,
        provider_cost=provider.config,
        payload=_estimate_payload_from_body(body),
    )


@router.get("/v1/openai/{capability}/v1/{path:path}")
@router.post("/v1/openai/{capability}/v1/{path:path}")
@router.put("/v1/openai/{capability}/v1/{path:path}")
@router.delete("/v1/openai/{capability}/v1/{path:path}")
@router.patch("/v1/openai/{capability}/v1/{path:path}")
@router.options("/v1/openai/{capability}/v1/{path:path}")
async def openai_proxy(
    capability: PathId,
    path: PathId,
    request: Request,
    pool: Any = Depends(_pool),
    capability_repo: CapabilityRepository = Depends(_capability_repo),
    provider_repo: ProviderRepository = Depends(_provider_repo),
    workload_repo: WorkloadRepository = Depends(_workload_repo),
    budget_gate: BudgetGate = Depends(_budget_gate),
) -> Response:
    started_at = time.perf_counter()
    try:
        validate_openai_proxy_path(path)
    except ValueError as exc:
        raise InvalidProxyPath(str(exc)) from exc
    resolved_capability, _chain, providers = await _resolve_provider_chain(
        capability,
        capability_repo,
        provider_repo,
    )

    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers["x-pitwall-capability"] = capability
    headers["x-pitwall-trace"] = "openai-proxy"

    drill_mode = headers.get("x-pitwall-drill", "").lower()
    drill_providers = (
        providers[1:] if drill_mode == "skip-primary" and len(providers) > 1 else providers
    )

    provider = drill_providers[0] if drill_providers else None
    if provider is None:
        raise ProviderUnavailable(capability)

    cost_quote = _quote_proxy_cost(resolved_capability, provider, body)
    cost_estimate_usd = cost_quote.upper_bound()
    admission = await budget_gate.try_launch_admission(
        capability_id=resolved_capability.id,
        provider_id=provider.id,
        estimate_usd=cost_quote,
        workload_type=_WORKLOAD_TYPE_OPENAI_PASSTHROUGH,
    )
    workload_id = admission.workload_id

    timeout = httpx.Timeout(330.0, connect=10.0)
    client = httpx.AsyncClient(timeout=timeout)
    try:
        await transition_to_running(workload_repo, workload_id)
    except Exception:  # reason: ledger transition is best-effort; proxying continues
        log.debug("workload running transition failed for %s", workload_id, exc_info=True)

    try:
        result = await execute_openai_with_fallback(
            OpenAIProxyRequest(
                method=request.method,
                path=_path_with_query(path, request),
                headers=headers,
                body=body,
                client=client,
                fallback_budget_s=_OPENAI_FALLBACK_BUDGET_S,
            ),
            drill_providers,
        )
    except OpenAIProxyExecutionError as exc:
        await client.aclose()
        execution_ms = int((time.perf_counter() - started_at) * 1000)
        emit_inference_trace(
            workload_id=workload_id,
            capability_name=capability,
            provider_id=provider.id,
            provider_type=provider.provider_type.value,
            runpod_endpoint_id=provider.runpod_endpoint_id,
            cost_estimate_usd=float(cost_estimate_usd),
            input_bytes=len(body) if body else 0,
            output_bytes=0,
            execution_ms=execution_ms,
            status="error",
            error=exc.cause or exc,
        )
        try:
            await transition_to_failed(
                workload_repo,
                workload_id,
                execution_ms=execution_ms,
                error={
                    "error": str(exc),
                    "attempted_providers": list(exc.attempted_provider_ids),
                    "attempted_errors": exc.attempted_errors,
                },
                fallback_chain=list(exc.attempted_provider_ids),
            )
        except Exception:  # reason: failure bookkeeping must not mask the original upstream error
            log.debug("workload failed transition failed for %s", workload_id, exc_info=True)
        raise ProviderUnavailable(
            capability,
            chain=list(exc.attempted_provider_ids),
        ) from exc

    upstream_response = result.response
    provider = result.provider
    execution_ms = int((time.perf_counter() - started_at) * 1000)
    output_bytes = _content_length(upstream_response)

    trace_id = emit_inference_trace(
        workload_id=workload_id,
        capability_name=capability,
        provider_id=provider.id,
        provider_type=provider.provider_type.value,
        runpod_endpoint_id=provider.runpod_endpoint_id,
        cost_estimate_usd=float(cost_estimate_usd),
        input_bytes=len(body) if body else 0,
        output_bytes=output_bytes,
        execution_ms=execution_ms,
        status="success" if upstream_response.status_code < 500 else "error",
    )

    try:
        await transition_to_completed(
            workload_repo,
            workload_id,
            execution_ms=execution_ms,
            output_bytes=output_bytes,
            fallback_chain=list(result.attempted_provider_ids),
        )
    except Exception:  # reason: completion bookkeeping must not fail the delivered response
        log.debug("workload completed transition failed for %s", workload_id, exc_info=True)

    response_headers = dict(upstream_response.headers)
    response_headers["X-Pitwall-Workload-ID"] = workload_id
    response_headers["X-Pitwall-Capability"] = capability
    response_headers["X-Pitwall-Provider-ID"] = provider.id
    if isinstance(trace_id, str):
        response_headers["X-Pitwall-Trace"] = trace_id

    return StreamingResponse(
        _relay_upstream_bytes(upstream_response, client),
        status_code=upstream_response.status_code,
        headers=response_headers,
    )


__all__ = ["router"]
