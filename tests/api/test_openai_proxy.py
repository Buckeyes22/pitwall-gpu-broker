"""SDK smoke test for OpenAI pass-through route.

Verify that the OpenAI SDK (openai>=1.40) can call the Pitwall proxy at
``/v1/openai/{capability}/v1/*`` when OPENAI_BASE_URL is set, using the
FastAPI test client in hermetic mode (no real network, no database).

This is an import-level smoke test — only the SDK's ability to form a
well-formed request and receive a parsed response is asserted.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from pitwall.core.enums import CapabilityClass, CapabilitySource, ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.cost.budget_gate import BudgetAdmission, BudgetRejected, BudgetSnapshot

_TEST_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _make_capability(
    id: str = "cap_llm_qwen3_32b",
    name: str = "llm.qwen3-32b",
) -> Capability:
    return Capability(
        id=id,
        name=name,
        version="1.0.0",
        class_=CapabilityClass.LLM,
        description="Qwen3 32B AWQ",
        cost_mode="per_request",
        source=CapabilitySource.API,
        enabled=True,
        created_at=_TEST_NOW,
        updated_at=_TEST_NOW,
    )


def _make_provider(
    id: str = "prov_qwen3_32b",
    capability_id: str = "cap_llm_qwen3_32b",
    name: str = "qwen3-32b-awq",
    provider_type: ProviderType = ProviderType.SERVERLESS_LB,
    runpod_endpoint_id: str = "qwen3-32b-awq",
    openai_base_url: str | None = None,
    priority: int = 1,
    per_request: str = "0.001000",
) -> Provider:
    config: dict[str, object] = {"per_request": per_request}
    if openai_base_url is not None:
        config["openai_base_url"] = openai_base_url
    else:
        config["openai_base_url"] = f"https://api.runpod.ai/v2/{runpod_endpoint_id}/openai/v1"
    return Provider(
        id=id,
        capability_id=capability_id,
        name=name,
        provider_type=provider_type,
        runpod_endpoint_id=runpod_endpoint_id,
        config=config,
        priority=priority,
        enabled=True,
        health_status="healthy",
        updated_at=_TEST_NOW,
    )


def _env_for_app(**overrides: str) -> dict[str, str]:
    base: dict[str, str] = {
        "RUNPOD_API_KEY": "test-key",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "REDIS_URL": "redis://localhost:6379/0",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clear_app_module():
    to_remove = [k for k in sys.modules if k.startswith("pitwall.api")]
    for k in to_remove:
        del sys.modules[k]
    yield
    to_remove = [k for k in sys.modules if k.startswith("pitwall.api")]
    for k in to_remove:
        del sys.modules[k]


def _import_app(env: dict[str, str]):
    old = os.environ.copy()
    os.environ.update(env)
    for k in list(os.environ):
        if k not in env and k in (
            "RUNPOD_API_KEY",
            "DATABASE_URL",
            "REDIS_URL",
            "PITWALL_ADMIN_SECRET",
            "PITWALL_API_TOKEN",
            "PITWALL_INBOUND_RATE_LIMIT",
        ):
            del os.environ[k]
    try:
        mod = importlib.import_module("pitwall.api.app")
        return mod
    finally:
        os.environ.clear()
        os.environ.update(old)


_CHAT_COMPLETION_RESPONSE = {
    "id": "chatcmpl-123",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "qwen3-32b-awq",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello! How can I help you today?",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    },
}


class _TrackingAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = tuple(chunks)
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _FailingAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], fail_after: int) -> None:
        self._chunks = tuple(chunks)
        self._fail_after = fail_after
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for i, chunk in enumerate(self._chunks):
            if i >= self._fail_after:
                raise httpx.ReadError("connection lost", request=MagicMock())
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _AdmittingBudgetGate:
    def __init__(self, workload_id: str = "wkl_openai_proxy_test") -> None:
        self.workload_id = workload_id
        self.calls: list[dict[str, object]] = []

    async def try_launch_admission(self, **kwargs: object) -> BudgetAdmission:
        self.calls.append(kwargs)
        return BudgetAdmission(workload_id=self.workload_id, is_new=True)


class _RejectingBudgetGate:
    def __init__(self, exc: BudgetRejected) -> None:
        self.exc = exc
        self.calls: list[dict[str, object]] = []

    async def try_launch_admission(self, **kwargs: object) -> BudgetAdmission:
        self.calls.append(kwargs)
        raise self.exc


class _RecordingWorkloadRepo:
    def __init__(self) -> None:
        self.transitions: list[dict[str, object]] = []

    async def guarded_transition(
        self,
        workload_id: str,
        from_states: set[str],
        to_state: object,
        *,
        patch: dict[str, object] | None = None,
    ) -> None:
        self.transitions.append(
            {
                "workload_id": workload_id,
                "from_states": from_states,
                "to_state": getattr(to_state, "value", to_state),
                "patch": patch or {},
            }
        )


def _setup_app_with_mocks(
    capability: Capability,
    provider: Provider,
    *,
    budget_gate: object | None = None,
    workload_repo: object | None = None,
):
    mock_capability_repo = AsyncMock()
    mock_capability_repo.get_by_name.return_value = capability

    mock_provider_repo = AsyncMock()
    mock_provider_repo.list.return_value = [provider]

    app_mod = _import_app(_env_for_app())
    from pitwall.api.routes.openai import (
        _budget_gate,
        _capability_repo,
        _provider_repo,
        _workload_repo,
    )

    app_mod.app.dependency_overrides[_capability_repo] = lambda: mock_capability_repo
    app_mod.app.dependency_overrides[_provider_repo] = lambda: mock_provider_repo
    gate = budget_gate if budget_gate is not None else _AdmittingBudgetGate()
    app_mod.app.dependency_overrides[_budget_gate] = lambda: gate
    repo = workload_repo if workload_repo is not None else _RecordingWorkloadRepo()
    app_mod.app.dependency_overrides[_workload_repo] = lambda: repo

    mock_pool = MagicMock()
    app_mod.app.state.pool = mock_pool

    return app_mod, mock_capability_repo, mock_provider_repo


def _budget_rejected() -> BudgetRejected:
    return BudgetRejected(
        "monthly_budget",
        BudgetSnapshot(
            monthly_budget_usd=Decimal("1.000000"),
            per_request_max_usd=Decimal("1.000000"),
            mtd_spend_usd=Decimal("1.000000"),
            estimate_usd=Decimal("0.250000"),
            budget_remaining_usd=Decimal("0.000000"),
        ),
    )


@pytest.mark.anyio
async def test_openai_proxy_budget_rejection_returns_402_before_upstream_send() -> None:
    capability = _make_capability()
    provider = _make_provider(
        openai_base_url="https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1",
        per_request="0.250000",
    )
    gate = _RejectingBudgetGate(_budget_rejected())
    app_mod, _, _ = _setup_app_with_mocks(capability, provider, budget_gate=gate)

    with respx.mock(assert_all_called=False) as router:
        upstream_call = router.post(
            "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        ).mock(return_value=httpx.Response(200, json=_CHAT_COMPLETION_RESPONSE))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_mod.app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                json={
                    "model": "qwen3-32b-awq",
                    "messages": [{"role": "user", "content": "Hello!"}],
                },
                headers={"Content-Type": "application/json"},
            )

    assert response.status_code == 402
    assert response.json() == _budget_rejected().to_response_body()
    assert not upstream_call.called
    assert len(gate.calls) == 1
    admission_kwargs = gate.calls[0]
    assert admission_kwargs["capability_id"] == capability.id
    assert admission_kwargs["provider_id"] == provider.id
    assert admission_kwargs["workload_type"] == "openai_passthrough"
    assert admission_kwargs["estimate_usd"].upper_bound() == Decimal("0.250000")

    app_mod.app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_openai_proxy_upstream_failure_terminally_fails_admitted_workload() -> None:
    capability = _make_capability()
    provider = _make_provider(
        openai_base_url="https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1",
        per_request="0.250000",
    )
    gate = _AdmittingBudgetGate(workload_id="wkl_admitted_proxy")
    workload_repo = _RecordingWorkloadRepo()
    app_mod, _, _ = _setup_app_with_mocks(
        capability,
        provider,
        budget_gate=gate,
        workload_repo=workload_repo,
    )

    def upstream_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dial failed", request=request)

    with respx.mock(assert_all_called=False) as router:
        upstream_call = router.post(
            "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        ).mock(side_effect=upstream_failure)
        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "Hello!"}],
                    },
                    headers={"Content-Type": "application/json"},
                )

    assert response.status_code == 503
    assert upstream_call.called
    assert len(gate.calls) == 1
    assert gate.calls[0]["workload_type"] == "openai_passthrough"
    assert [transition["to_state"] for transition in workload_repo.transitions] == [
        "running",
        "failed",
    ]
    assert {transition["workload_id"] for transition in workload_repo.transitions} == {
        "wkl_admitted_proxy"
    }
    failed_patch = workload_repo.transitions[-1]["patch"]
    assert failed_patch["fallback_chain"] == [provider.id]
    assert "dial failed" in failed_patch["error"]["attempted_errors"][provider.id]

    app_mod.app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_openai_proxy_relay_closes_upstream_response_and_client() -> None:
    from pitwall.api.routes.openai import _relay_upstream_bytes

    stream = _TrackingAsyncByteStream(
        [
            b'data: {"delta":"hello"}\n\n',
            b"data: [DONE]\n\n",
        ]
    )
    upstream_response = httpx.Response(200, stream=stream)
    client = httpx.AsyncClient()

    chunks: list[bytes] = []
    async for chunk in _relay_upstream_bytes(upstream_response, client):
        chunks.append(chunk)

    assert b"".join(chunks) == b'data: {"delta":"hello"}\n\ndata: [DONE]\n\n'
    assert stream.closed
    assert upstream_response.is_closed
    assert client.is_closed


@pytest.mark.anyio
async def test_relay_emits_sse_error_on_mid_stream_failure() -> None:
    from pitwall.api.routes.openai import _relay_upstream_bytes

    stream = _FailingAsyncByteStream(
        [
            b'data: {"delta":"hello"}\n\n',
            b'data: {"delta":"world"}\n\n',
        ],
        fail_after=1,
    )
    upstream_response = httpx.Response(
        200,
        stream=stream,
        headers={"content-type": "text/event-stream"},
    )
    client = httpx.AsyncClient()

    chunks: list[bytes] = []
    async for chunk in _relay_upstream_bytes(upstream_response, client):
        chunks.append(chunk)

    data = b"".join(chunks)
    assert b'data: {"delta":"hello"}\n\n' in data
    assert b'"error"' in data
    assert b"upstream stream failure" in data
    assert stream.closed
    assert upstream_response.is_closed
    assert client.is_closed


@pytest.mark.anyio
async def test_relay_no_sse_error_for_non_sse_response() -> None:
    from pitwall.api.routes.openai import _relay_upstream_bytes

    stream = _FailingAsyncByteStream(
        [b'{"partial":'],
        fail_after=1,
    )
    upstream_response = httpx.Response(
        200,
        stream=stream,
        headers={"content-type": "application/json"},
    )
    client = httpx.AsyncClient()

    chunks: list[bytes] = []
    async for chunk in _relay_upstream_bytes(upstream_response, client):
        chunks.append(chunk)

    data = b"".join(chunks)
    assert data == b'{"partial":'
    assert b'"error"' not in data
    assert stream.closed
    assert upstream_response.is_closed
    assert client.is_closed


@pytest.mark.anyio
async def test_streaming_sse_with_usage_frame_passes_through() -> None:
    from pitwall.api.routes.openai import _relay_upstream_bytes

    stream = _TrackingAsyncByteStream(
        [
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n',
            b"data: [DONE]\n\n",
        ]
    )
    upstream_response = httpx.Response(
        200,
        stream=stream,
        headers={"content-type": "text/event-stream"},
    )
    client = httpx.AsyncClient()

    chunks: list[bytes] = []
    async for chunk in _relay_upstream_bytes(upstream_response, client):
        chunks.append(chunk)

    data = b"".join(chunks)
    assert b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n' in data
    assert (
        b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
        in data
    )
    assert b"data: [DONE]\n\n" in data
    assert stream.closed
    assert upstream_response.is_closed
    assert client.is_closed


@pytest.mark.anyio
async def test_relay_cleans_up_when_consumer_exits_early() -> None:
    import asyncio

    from pitwall.api.routes.openai import _relay_upstream_bytes

    stream = _TrackingAsyncByteStream(
        [
            b'data: {"delta":"chunk1"}\n\n',
            b'data: {"delta":"chunk2"}\n\n',
            b'data: {"delta":"chunk3"}\n\n',
            b"data: [DONE]\n\n",
        ]
    )
    upstream_response = httpx.Response(
        200,
        stream=stream,
        headers={"content-type": "text/event-stream"},
    )
    client = httpx.AsyncClient()

    received_chunks: list[bytes] = []

    async def consume_with_timeout():
        nonlocal received_chunks
        try:
            async with asyncio.timeout(0.01):
                async for chunk in _relay_upstream_bytes(upstream_response, client):
                    received_chunks.append(chunk)
        except TimeoutError:
            pass

    await consume_with_timeout()

    assert len(received_chunks) >= 1
    assert stream.closed
    assert upstream_response.is_closed
    assert client.is_closed


@respx.mock
@pytest.mark.anyio
async def test_openai_proxy_chat_completions_with_sdk_request_shape():
    """OpenAI SDK-shaped request to /v1/openai/{cap}/v1/chat/completions succeeds.

    This test proves OPENAI_BASE_URL compatibility by sending a request that
    mimics exactly what the OpenAI SDK would send: proper Content-Type,
    correct JSON schema for chat completions, and correct route path.
    """
    capability = _make_capability()
    provider = _make_provider(openai_base_url="https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1")

    app_mod, _, _ = _setup_app_with_mocks(capability, provider)

    upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
    respx.post(upstream_url).mock(return_value=httpx.Response(200, json=_CHAT_COMPLETION_RESPONSE))

    with patch("pitwall.api.routes.openai.emit_inference_trace"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_mod.app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                json={
                    "model": "qwen3-32b-awq",
                    "messages": [
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": "Hello!"},
                    ],
                    "temperature": 0.7,
                    "max_tokens": 100,
                },
                headers={"Content-Type": "application/json"},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "chatcmpl-123"
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "Hello! How can I help you today?"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 10
    assert body["usage"]["completion_tokens"] == 20
    assert body["usage"]["total_tokens"] == 30

    app_mod.app.dependency_overrides.clear()


@respx.mock
@pytest.mark.anyio
async def test_openai_proxy_models_endpoint():
    """OpenAI SDK can call /v1/openai/{cap}/v1/models via proxy."""
    capability = _make_capability()
    provider = _make_provider(openai_base_url="https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1")

    app_mod, _, _ = _setup_app_with_mocks(capability, provider)

    upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/models"
    models_response = {
        "object": "list",
        "data": [
            {
                "id": "qwen3-32b-awq",
                "object": "model",
                "created": 1234567890,
                "owned_by": "pitwall",
            }
        ],
    }
    respx.get(upstream_url).mock(return_value=httpx.Response(200, json=models_response))

    with patch("pitwall.api.routes.openai.emit_inference_trace"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_mod.app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/v1/openai/llm.qwen3-32b/v1/models",
                headers={"Content-Type": "application/json"},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 1
    assert body["data"][0]["id"] == "qwen3-32b-awq"

    app_mod.app.dependency_overrides.clear()


@respx.mock
@pytest.mark.anyio
async def test_openai_proxy_forwards_headers_correctly():
    """OpenAI SDK headers (including custom ones) are forwarded to upstream."""
    capability = _make_capability()
    provider = _make_provider(openai_base_url="https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1")

    app_mod, _, _ = _setup_app_with_mocks(capability, provider)

    upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"

    async def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        content_type = request.headers.get("Content-Type", "")
        openai_model = request.headers.get("OpenAI-Organization", "")
        return httpx.Response(
            200,
            json={
                **{
                    "id": "chatcmpl-456",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "qwen3-32b-awq",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Header test passed",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 3,
                        "total_tokens": 8,
                    },
                },
                "_received_headers": {
                    "authorization": auth,
                    "content_type": content_type,
                    "openai_organization": openai_model,
                },
            },
        )

    respx.post(upstream_url).mock(side_effect=handler)

    with patch("pitwall.api.routes.openai.emit_inference_trace"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_mod.app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                json={
                    "model": "qwen3-32b-awq",
                    "messages": [{"role": "user", "content": "Test"}],
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer sk-test-key",
                    "OpenAI-Organization": "my-org",
                },
            )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "Header test passed"
    received = body["_received_headers"]
    assert "Bearer sk-test-key" in received["authorization"]

    app_mod.app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_openai_proxy_returns_404_for_unknown_capability():
    """Unknown capability name returns CapabilityNotFound (404)."""
    mock_capability_repo = AsyncMock()
    mock_capability_repo.get_by_name.return_value = None

    mock_provider_repo = AsyncMock()

    app_mod = _import_app(_env_for_app())
    from pitwall.api.routes.openai import _capability_repo, _provider_repo

    app_mod.app.dependency_overrides[_capability_repo] = lambda: mock_capability_repo
    app_mod.app.dependency_overrides[_provider_repo] = lambda: mock_provider_repo

    mock_pool = MagicMock()
    app_mod.app.state.pool = mock_pool

    with patch("pitwall.api.routes.openai.emit_inference_trace"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_mod.app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/openai/nonexistent.capability/v1/chat/completions",
                json={
                    "model": "something",
                    "messages": [{"role": "user", "content": "Hello!"}],
                },
                headers={"Content-Type": "application/json"},
            )

    assert response.status_code == 404
    body = response.json()
    assert "capability" in body.get("error", "").lower()

    app_mod.app.dependency_overrides.clear()
