"""Unit tests for execute_openai_with_fallback.

Directly exercises the fallback executor without the FastAPI HTTP stack,
covering:
  - 5xx triggers fallback to next provider
  - 4xx returns immediately (no fallback)
  - Transport failure before headers triggers fallback
  - Attempt chain is capped at three attempts
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from pitwall.core.enums import ProviderType
from pitwall.core.models import Provider
from pitwall.routing.fallback import (
    DEFAULT_OPENAI_FALLBACK_BUDGET_S,
    OpenAIProxyExecutionError,
    OpenAIProxyRequest,
    OpenAIProxyResult,
    execute_openai_with_fallback,
)

_TEST_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _make_provider(
    provider_id: str,
    endpoint_id: str,
    *,
    priority: int = 1,
    fallback_chain: list[str] | None = None,
) -> Provider:
    config: dict[str, object] = {
        "openai_base_url": f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1",
    }
    if fallback_chain is not None:
        config["fallback_chain"] = fallback_chain
    return Provider(
        id=provider_id,
        capability_id="cap_llm_qwen3_32b",
        name=provider_id,
        provider_type=ProviderType.PUBLIC_ENDPOINT,
        runpod_endpoint_id=endpoint_id,
        config=config,
        priority=priority,
        enabled=True,
        health_status="healthy",
        updated_at=_TEST_NOW,
    )


def _request_ctx(client: httpx.AsyncClient) -> OpenAIProxyRequest:
    return OpenAIProxyRequest(
        method="POST",
        path="chat/completions",
        headers={"content-type": "application/json"},
        body=b'{"model":"test","messages":[]}',
        client=client,
        fallback_budget_s=DEFAULT_OPENAI_FALLBACK_BUDGET_S,
    )


@respx.mock
@pytest.mark.anyio
async def test_5xx_triggers_fallback_to_next_provider():
    primary = _make_provider("prov_a", "ep_a", priority=1, fallback_chain=["prov_b"])
    secondary = _make_provider("prov_b", "ep_b", priority=2)

    respx.post("https://api.runpod.ai/v2/ep_a/openai/v1/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": "unavailable"})
    )
    respx.post("https://api.runpod.ai/v2/ep_b/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"id": "chatcmpl-ok"})
    )

    async with httpx.AsyncClient() as client:
        result = await execute_openai_with_fallback(
            _request_ctx(client),
            [primary, secondary],
        )

    assert isinstance(result, OpenAIProxyResult)
    assert result.response.status_code == 200
    assert result.provider_id == "prov_b"
    assert result.attempted_provider_ids == ("prov_a", "prov_b")
    assert result.attempt_count == 2


@respx.mock
@pytest.mark.anyio
async def test_4xx_returns_immediately_no_fallback():
    primary = _make_provider("prov_a", "ep_a", priority=1, fallback_chain=["prov_b"])
    secondary = _make_provider("prov_b", "ep_b", priority=2)

    respx.post("https://api.runpod.ai/v2/ep_a/openai/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    secondary_route = respx.post("https://api.runpod.ai/v2/ep_b/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"id": "should-not-run"})
    )

    async with httpx.AsyncClient() as client:
        result = await execute_openai_with_fallback(
            _request_ctx(client),
            [primary, secondary],
        )

    assert isinstance(result, OpenAIProxyResult)
    assert result.response.status_code == 401
    assert result.provider_id == "prov_a"
    assert result.attempted_provider_ids == ("prov_a",)
    assert result.attempt_count == 1
    assert not secondary_route.called


@respx.mock
@pytest.mark.anyio
async def test_transport_failure_before_headers_triggers_fallback():
    primary = _make_provider("prov_a", "ep_a", priority=1, fallback_chain=["prov_b"])
    secondary = _make_provider("prov_b", "ep_b", priority=2)

    def primary_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    respx.post("https://api.runpod.ai/v2/ep_a/openai/v1/chat/completions").mock(
        side_effect=primary_handler
    )
    respx.post("https://api.runpod.ai/v2/ep_b/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"id": "chatcmpl-ok"})
    )

    async with httpx.AsyncClient() as client:
        result = await execute_openai_with_fallback(
            _request_ctx(client),
            [primary, secondary],
        )

    assert isinstance(result, OpenAIProxyResult)
    assert result.response.status_code == 200
    assert result.provider_id == "prov_b"
    assert result.attempted_provider_ids == ("prov_a", "prov_b")


@respx.mock
@pytest.mark.anyio
async def test_attempt_chain_capped_at_three():
    p1 = _make_provider("prov_1", "ep_1", priority=1, fallback_chain=["prov_2", "prov_3", "prov_4"])
    p2 = _make_provider("prov_2", "ep_2", priority=2)
    p3 = _make_provider("prov_3", "ep_3", priority=3)
    p4 = _make_provider("prov_4", "ep_4", priority=4)

    respx.post("https://api.runpod.ai/v2/ep_1/openai/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"err": "1"})
    )
    respx.post("https://api.runpod.ai/v2/ep_2/openai/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"err": "2"})
    )
    respx.post("https://api.runpod.ai/v2/ep_3/openai/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"err": "3"})
    )
    fourth_route = respx.post("https://api.runpod.ai/v2/ep_4/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"id": "should-not-run"})
    )

    async with httpx.AsyncClient() as client:
        result = await execute_openai_with_fallback(
            _request_ctx(client),
            [p1, p2, p3, p4],
        )

    assert isinstance(result, OpenAIProxyResult)
    assert result.response.status_code == 500
    assert result.provider_id == "prov_3"
    assert result.attempted_provider_ids == ("prov_1", "prov_2", "prov_3")
    assert result.attempt_count == 3
    assert not fourth_route.called


@respx.mock
@pytest.mark.anyio
async def test_all_transport_failures_raise_execution_error():
    primary = _make_provider("prov_a", "ep_a", priority=1, fallback_chain=["prov_b"])
    secondary = _make_provider("prov_b", "ep_b", priority=2)

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dial failed", request=request)

    respx.post("https://api.runpod.ai/v2/ep_a/openai/v1/chat/completions").mock(side_effect=fail)
    respx.post("https://api.runpod.ai/v2/ep_b/openai/v1/chat/completions").mock(side_effect=fail)

    async with httpx.AsyncClient() as client:
        with pytest.raises(OpenAIProxyExecutionError) as exc_info:
            await execute_openai_with_fallback(
                _request_ctx(client),
                [primary, secondary],
            )

    assert exc_info.value.attempted_provider_ids == ("prov_a", "prov_b")
    assert exc_info.value.cause is not None
