"""Comprehensive proxy route behavior tests.

Cover method, path, query, body, status, headers, and no-envelope behavior.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from pitwall.core.enums import CapabilityClass, CapabilitySource, ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.cost.budget_gate import BudgetAdmission

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
    *,
    id: str = "prov_qwen3_32b",
    endpoint_id: str = "qwen3-32b-awq",
    priority: int = 1,
) -> Provider:
    config: dict[str, object] = {
        "openai_base_url": f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1",
        "per_request": "0.001000",
    }
    return Provider(
        id=id,
        capability_id="cap_llm_qwen3_32b",
        name=id,
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id=endpoint_id,
        config=config,
        priority=priority,
        enabled=True,
        health_status="healthy",
        updated_at=_TEST_NOW,
    )


def _env_for_app() -> dict[str, str]:
    return {
        "RUNPOD_API_KEY": "test-key",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "REDIS_URL": "redis://localhost:6379/0",
    }


@pytest.fixture(autouse=True)
def _clear_app_module():
    to_remove = [k for k in sys.modules if k.startswith("pitwall.api")]
    for key in to_remove:
        del sys.modules[key]
    yield
    to_remove = [k for k in sys.modules if k.startswith("pitwall.api")]
    for key in to_remove:
        del sys.modules[key]


def _import_app():
    old = os.environ.copy()
    env = _env_for_app()
    os.environ.update(env)
    for key in list(os.environ):
        if key not in env and key in (
            "RUNPOD_API_KEY",
            "DATABASE_URL",
            "REDIS_URL",
            "PITWALL_ADMIN_SECRET",
            "PITWALL_API_TOKEN",
            "PITWALL_INBOUND_RATE_LIMIT",
        ):
            del os.environ[key]
    try:
        return importlib.import_module("pitwall.api.app")
    finally:
        os.environ.clear()
        os.environ.update(old)


def _setup_app_with_provider(provider: Provider | None = None):
    mock_capability_repo = AsyncMock()
    mock_capability_repo.get_by_name.return_value = _make_capability()

    mock_provider_repo = AsyncMock()
    mock_provider_repo.list.return_value = [provider] if provider else []

    app_mod = _import_app()
    from pitwall.api.routes.openai import (
        _budget_gate,
        _capability_repo,
        _provider_repo,
        _workload_repo,
    )

    app_mod.app.dependency_overrides[_capability_repo] = lambda: mock_capability_repo
    app_mod.app.dependency_overrides[_provider_repo] = lambda: mock_provider_repo
    budget_gate = AsyncMock()
    budget_gate.try_launch_admission.return_value = BudgetAdmission(
        workload_id="wkl_openai_routes_test",
        is_new=True,
    )
    workload_repo = AsyncMock()
    workload_repo.guarded_transition.return_value = None
    app_mod.app.dependency_overrides[_budget_gate] = lambda: budget_gate
    app_mod.app.dependency_overrides[_workload_repo] = lambda: workload_repo
    app_mod.app.state.pool = MagicMock()
    return app_mod


class TestProxyMethod:
    """Test different HTTP methods are passed through correctly."""

    @respx.mock
    @pytest.mark.anyio
    async def test_get_method_passed_to_upstream(self):
        """GET requests are forwarded with correct method."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/models"
        received_method: str = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_method
            received_method = request.method
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "model-1", "object": "model"}],
                },
            )

        respx.get(upstream_url).mock(side_effect=handler)

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.get("/v1/openai/llm.qwen3-32b/v1/models")

        assert response.status_code == 200
        assert received_method == "GET"
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_post_method_passed_to_upstream(self):
        """POST requests are forwarded with correct method."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        received_method: str = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_method
            received_method = request.method
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "qwen3-32b-awq",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
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
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 200
        assert received_method == "POST"
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_put_method_passed_to_upstream(self):
        """PUT requests are forwarded with correct method."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/models/model-1"
        received_method: str = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_method
            received_method = request.method
            return httpx.Response(200, json={"id": "model-1", "object": "model"})

        respx.put(upstream_url).mock(side_effect=handler)

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.put(
                    "/v1/openai/llm.qwen3-32b/v1/models/model-1",
                    json={"object": "model"},
                )

        assert response.status_code == 200
        assert received_method == "PUT"
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_delete_method_passed_to_upstream(self):
        """DELETE requests are forwarded with correct method."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/files/file-1"
        received_method: str = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_method
            received_method = request.method
            return httpx.Response(200, json={"id": "file-1", "deleted": True})

        respx.delete(upstream_url).mock(side_effect=handler)

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.delete("/v1/openai/llm.qwen3-32b/v1/files/file-1")

        assert response.status_code == 200
        assert received_method == "DELETE"
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_patch_method_passed_to_upstream(self):
        """PATCH requests are forwarded with correct method."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/files/file-1"
        received_method: str = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_method
            received_method = request.method
            return httpx.Response(200, json={"id": "file-1", "object": "file"})

        respx.patch(upstream_url).mock(side_effect=handler)

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.patch(
                    "/v1/openai/llm.qwen3-32b/v1/files/file-1",
                    json={"object": "file"},
                )

        assert response.status_code == 200
        assert received_method == "PATCH"
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_options_method_passed_to_upstream(self):
        """OPTIONS requests are forwarded with correct method."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        received_method: str = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_method
            received_method = request.method
            return httpx.Response(
                200,
                headers={"Allow": "GET, POST, OPTIONS"},
                json={},
            )

        respx.options(upstream_url).mock(side_effect=handler)

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.options("/v1/openai/llm.qwen3-32b/v1/chat/completions")

        assert response.status_code == 200
        assert received_method == "OPTIONS"
        app_mod.app.dependency_overrides.clear()


class TestProxyPath:
    """Test different API paths are routed correctly."""

    @respx.mock
    @pytest.mark.anyio
    async def test_chat_completions_path(self):
        """/v1/chat/completions route works."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        received_path: str = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_path
            received_path = request.url.path
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "qwen3-32b-awq",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
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
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 200
        assert "/chat/completions" in received_path
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_embeddings_path(self):
        """/v1/embeddings route works."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/embeddings"
        received_path: str = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_path
            received_path = request.url.path
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "object": "embedding",
                            "embedding": [0.1, 0.2, 0.3],
                            "index": 0,
                        }
                    ],
                    "model": "qwen3-32b-awq",
                },
            )

        respx.post(upstream_url).mock(side_effect=handler)

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/embeddings",
                    json={
                        "model": "qwen3-32b-awq",
                        "input": "hello world",
                    },
                )

        assert response.status_code == 200
        assert "/embeddings" in received_path
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_models_path(self):
        """/v1/models route works."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/models"
        received_path: str = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_path
            received_path = request.url.path
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "qwen3-32b-awq",
                            "object": "model",
                            "created": 1234567890,
                            "owned_by": "pitwall",
                        }
                    ],
                },
            )

        respx.get(upstream_url).mock(side_effect=handler)

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.get("/v1/openai/llm.qwen3-32b/v1/models")

        assert response.status_code == 200
        assert "/models" in received_path
        app_mod.app.dependency_overrides.clear()


class TestProxyQuery:
    """Test query string handling."""

    @respx.mock
    @pytest.mark.anyio
    async def test_query_params_passed_to_upstream(self):
        """Query parameters are forwarded to upstream."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        received_query: str = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_query
            received_query = (
                request.url.query.decode("utf-8")
                if isinstance(request.url.query, bytes)
                else request.url.query
            )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "qwen3-32b-awq",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                },
            )

        respx.post(upstream_url).mock(side_effect=handler)

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions?stream=true",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 200
        assert "stream=true" in received_query
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_multiple_query_params_passed_to_upstream(self):
        """Multiple query parameters are forwarded correctly."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        received_query: str = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_query
            received_query = (
                request.url.query.decode("utf-8")
                if isinstance(request.url.query, bytes)
                else request.url.query
            )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "qwen3-32b-awq",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                },
            )

        respx.post(upstream_url).mock(side_effect=handler)

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions?stream=false&max_tokens=100",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 200
        assert "stream=false" in received_query
        assert "max_tokens=100" in received_query
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_empty_query_string_not_appended(self):
        """Empty query string does not append trailing question mark."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        received_query: str = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_query
            received_query = (
                request.url.query.decode("utf-8")
                if isinstance(request.url.query, bytes)
                else request.url.query or ""
            )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "qwen3-32b-awq",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
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
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 200
        assert received_query == ""
        app_mod.app.dependency_overrides.clear()


class TestProxyBody:
    """Test request body handling."""

    @respx.mock
    @pytest.mark.anyio
    async def test_json_body_passed_to_upstream(self):
        """JSON request body is forwarded verbatim."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        received_body: bytes = b""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_body
            received_body = request.content
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "qwen3-32b-awq",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                },
            )

        respx.post(upstream_url).mock(side_effect=handler)

        request_body = {
            "model": "qwen3-32b-awq",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.7,
            "max_tokens": 100,
        }

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json=request_body,
                )

        assert response.status_code == 200
        assert b'"temperature":0.7' in received_body or b'"temperature": 0.7' in received_body
        assert b'"max_tokens":100' in received_body or b'"max_tokens": 100' in received_body
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_empty_body_passed_to_upstream(self):
        """Empty request body is forwarded correctly."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        received_body: bytes = b""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_body
            received_body = request.content
            return httpx.Response(200, json={})

        respx.post(upstream_url).mock(side_effect=handler)

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    content=b"",
                )

        assert response.status_code == 200
        assert received_body == b""
        app_mod.app.dependency_overrides.clear()


class TestProxyStatus:
    """Test different upstream status codes are passed through."""

    @respx.mock
    @pytest.mark.anyio
    async def test_400_bad_request_passed_through(self):
        """400 status code is passed through to client."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(
                400,
                json={"error": {"message": "Invalid request", "type": "invalid_request_error"}},
            )
        )

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 400
        body = response.json()
        assert "error" in body
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_401_unauthorized_passed_through(self):
        """401 status code is passed through to client."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(
                401,
                json={"error": {"message": "Invalid API key", "type": "authentication_error"}},
            )
        )

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 401
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_403_forbidden_passed_through(self):
        """403 status code is passed through to client."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(
                403,
                json={"error": {"message": "Permission denied", "type": "permission_error"}},
            )
        )

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 403
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_404_not_found_passed_through(self):
        """404 status code is passed through to client."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(
                404,
                json={"error": {"message": "Model not found", "type": "invalid_request_error"}},
            )
        )

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 404
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_429_rate_limit_passed_through(self):
        """429 status code is passed through to client."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(
                429,
                json={"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}},
                headers={"Retry-After": "60"},
            )
        )

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 429
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_500_server_error_passed_through(self):
        """500 status code is passed through to client."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(
                500,
                json={"error": {"message": "Internal server error", "type": "server_error"}},
            )
        )

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 500
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_502_bad_gateway_passed_through(self):
        """502 status code is passed through to client."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(
                502,
                json={"error": {"message": "Bad gateway", "type": "server_error"}},
            )
        )

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 502
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_503_service_unavailable_passed_through(self):
        """503 status code is passed through to client."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(
                503,
                json={"error": {"message": "Service unavailable", "type": "server_error"}},
            )
        )

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 503
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_504_gateway_timeout_passed_through(self):
        """504 status code is passed through to client."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(
                504,
                json={"error": {"message": "Gateway timeout", "type": "server_error"}},
            )
        )

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 504
        app_mod.app.dependency_overrides.clear()


class TestProxyHeaders:
    """Test header handling."""

    @respx.mock
    @pytest.mark.anyio
    async def test_request_headers_forwarded(self):
        """Request headers are forwarded to upstream."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        received_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_headers
            received_headers = dict(request.headers)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "qwen3-32b-awq",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
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
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                    headers={
                        "Authorization": "Bearer sk-test-key",
                        "Content-Type": "application/json",
                        "OpenAI-Organization": "my-org",
                        "X-Request-ID": "req-123",
                    },
                )

        assert response.status_code == 200
        assert received_headers.get("authorization") == "Bearer sk-test-key"
        assert received_headers.get("content-type") == "application/json"
        assert received_headers.get("openai-organization") == "my-org"
        assert received_headers.get("x-request-id") == "req-123"
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_pitwall_headers_added_to_request(self):
        """Pitwall observability headers are added to request."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        received_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_headers
            received_headers = dict(request.headers)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "qwen3-32b-awq",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
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
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 200
        assert "x-pitwall-capability" in received_headers
        assert received_headers["x-pitwall-capability"] == "llm.qwen3-32b"
        assert "x-pitwall-trace" in received_headers
        assert received_headers["x-pitwall-trace"] == "openai-proxy"
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_response_headers_passed_through(self):
        """Response headers from upstream are passed through to client."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "qwen3-32b-awq",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                },
                headers={
                    "X-Request-ID": "upstream-req-123",
                    "X-RateLimit-Remaining": "100",
                },
            )
        )

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 200
        assert "x-request-id" in response.headers
        assert response.headers["x-request-id"] == "upstream-req-123"
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_pitwall_headers_added_to_response(self):
        """Pitwall-specific headers are added to response."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "qwen3-32b-awq",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                },
            )
        )

        with patch("pitwall.api.routes.openai.emit_inference_trace", return_value="trace-abc"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 200
        assert "x-pitwall-workload-id" in response.headers
        assert "x-pitwall-capability" in response.headers
        assert response.headers["x-pitwall-capability"] == "llm.qwen3-32b"
        assert "x-pitwall-trace" in response.headers
        assert response.headers["x-pitwall-trace"] == "trace-abc"
        app_mod.app.dependency_overrides.clear()


class TestProxyNoEnvelope:
    """Test that responses are passed through without envelope wrapping."""

    @respx.mock
    @pytest.mark.anyio
    async def test_chat_completion_response_not_wrapped(self):
        """Chat completion response is passed through as-is, not wrapped."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        upstream_response = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "qwen3-32b-awq",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

        respx.post(upstream_url).mock(return_value=httpx.Response(200, json=upstream_response))

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == "chatcmpl-123"
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "Hello!"
        assert (
            "data" not in body
            or body.get("data") is None
            or isinstance(body.get("data"), list) is False
        )
        assert "envelope" not in str(body).lower()
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_models_list_response_not_wrapped(self):
        """Models list response is passed through as-is."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/models"
        upstream_response = {
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

        respx.get(upstream_url).mock(return_value=httpx.Response(200, json=upstream_response))

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.get("/v1/openai/llm.qwen3-32b/v1/models")

        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "list"
        assert len(body["data"]) == 1
        assert body["data"][0]["id"] == "qwen3-32b-awq"
        assert "wrapper" not in str(body).lower()
        assert "envelope" not in str(body).lower()
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_error_response_not_wrapped(self):
        """Error response is passed through as-is without envelope."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        upstream_response = {
            "error": {
                "message": "Invalid API key",
                "type": "authentication_error",
                "code": "invalid_api_key",
            }
        }

        respx.post(upstream_url).mock(return_value=httpx.Response(401, json=upstream_response))

        with patch("pitwall.api.routes.openai.emit_inference_trace"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_mod.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/openai/llm.qwen3-32b/v1/chat/completions",
                    json={
                        "model": "qwen3-32b-awq",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert response.status_code == 401
        body = response.json()
        assert "error" in body
        assert body["error"]["message"] == "Invalid API key"
        assert "envelope" not in str(body).lower()
        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_streaming_sse_response_not_wrapped(self):
        """SSE streaming response chunks are passed through verbatim."""
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        upstream_url = "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n',
                headers={"content-type": "text/event-stream"},
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
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                )

        assert response.status_code == 200
        content = b"".join([chunk async for chunk in response.aiter_bytes()])
        assert b"data:" in content
        assert b"[DONE]" in content
        assert "envelope" not in content.decode("utf-8").lower()
        app_mod.app.dependency_overrides.clear()


class TestProxyPathSafety:
    """Unsafe proxy paths are rejected at the route with 400, never 500."""

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "bad_path",
        [
            "https://169.254.169.254/latest/meta-data",
            "//evil.example/steal",
            "..%2f..%2fadmin",
            "chat\\completions",
        ],
    )
    async def test_unsafe_path_returns_400(self, bad_path: str):
        provider = _make_provider()
        app_mod = _setup_app_with_provider(provider)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_mod.app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/v1/openai/llm.qwen3-32b/v1/{bad_path}",
                json={},
            )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_proxy_path"
        app_mod.app.dependency_overrides.clear()
