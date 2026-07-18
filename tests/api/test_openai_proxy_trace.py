"""Mocked Langfuse trace coverage for success, fallback, and stream error paths.

Verify that emit_inference_trace is called with the correct parameters
for each execution path through the OpenAI proxy.
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
    id: str,
    endpoint_id: str,
    priority: int,
    fallback_chain: list[str] | None = None,
) -> Provider:
    config: dict[str, object] = {
        "openai_base_url": f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1",
        "per_request": "0.001000",
    }
    if fallback_chain is not None:
        config["fallback_chain"] = fallback_chain
    return Provider(
        id=id,
        capability_id="cap_llm_qwen3_32b",
        name=id,
        provider_type=ProviderType.PUBLIC_ENDPOINT,
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


def _setup_app_with_providers(providers: list[Provider]):
    mock_capability_repo = AsyncMock()
    mock_capability_repo.get_by_name.return_value = _make_capability()

    mock_provider_repo = AsyncMock()
    mock_provider_repo.list.return_value = providers

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
        workload_id="wkl_openai_trace_test",
        is_new=True,
    )
    workload_repo = AsyncMock()
    workload_repo.guarded_transition.return_value = None
    app_mod.app.dependency_overrides[_budget_gate] = lambda: budget_gate
    app_mod.app.dependency_overrides[_workload_repo] = lambda: workload_repo
    app_mod.app.state.pool = MagicMock()
    return app_mod


async def _post_chat(app_mod) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_mod.app),
        base_url="http://test",
    ) as client:
        return await client.post(
            "/v1/openai/llm.qwen3-32b/v1/chat/completions",
            json={
                "model": "qwen3-32b-awq",
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers={"Content-Type": "application/json"},
        )


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


class TestSuccessPathTrace:
    """Verify emit_inference_trace is called correctly on successful inference."""

    @respx.mock
    @pytest.mark.anyio
    async def test_trace_emitted_with_success_status_on_200(self):
        """When upstream returns 200, trace is emitted with status='success'."""
        provider = _make_provider(
            id="prov_primary",
            endpoint_id="primary",
            priority=1,
        )
        app_mod = _setup_app_with_providers([provider])

        upstream_url = "https://api.runpod.ai/v2/primary/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(200, json=_CHAT_COMPLETION_RESPONSE)
        )

        mock_trace = MagicMock(return_value="trace_abc123")
        with patch("pitwall.api.routes.openai.emit_inference_trace", mock_trace):
            response = await _post_chat(app_mod)

        assert response.status_code == 200
        mock_trace.assert_called_once()
        call_kwargs = mock_trace.call_args.kwargs
        assert call_kwargs["status"] == "success"
        assert call_kwargs["capability_name"] == "llm.qwen3-32b"
        assert call_kwargs["provider_id"] == "prov_primary"
        assert call_kwargs["provider_type"] == "public_endpoint"

        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_trace_emitted_with_error_status_on_500(self):
        """When upstream returns 500, trace is emitted with status='error'."""
        provider = _make_provider(
            id="prov_primary",
            endpoint_id="primary",
            priority=1,
        )
        app_mod = _setup_app_with_providers([provider])

        upstream_url = "https://api.runpod.ai/v2/primary/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(500, json={"error": "internal server error"})
        )

        mock_trace = MagicMock(return_value="trace_abc123")
        with patch("pitwall.api.routes.openai.emit_inference_trace", mock_trace):
            response = await _post_chat(app_mod)

        assert response.status_code == 500
        mock_trace.assert_called_once()
        call_kwargs = mock_trace.call_args.kwargs
        assert call_kwargs["status"] == "error"

        app_mod.app.dependency_overrides.clear()


class TestFallbackPathTrace:
    """Verify emit_inference_trace is called correctly when fallback is used."""

    @respx.mock
    @pytest.mark.anyio
    async def test_trace_emitted_with_fallback_provider_on_fallback_success(self):
        """When primary fails and fallback succeeds, trace uses fallback provider."""
        primary = _make_provider(
            id="prov_primary",
            endpoint_id="primary",
            priority=1,
            fallback_chain=["prov_secondary"],
        )
        secondary = _make_provider(
            id="prov_secondary",
            endpoint_id="secondary",
            priority=2,
        )
        app_mod = _setup_app_with_providers([primary, secondary])

        respx.post("https://api.runpod.ai/v2/primary/openai/v1/chat/completions").mock(
            return_value=httpx.Response(503, json={"error": "primary unavailable"})
        )

        fallback_response = dict(_CHAT_COMPLETION_RESPONSE)
        fallback_response["id"] = "chatcmpl-fallback"
        respx.post("https://api.runpod.ai/v2/secondary/openai/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=fallback_response)
        )

        mock_trace = MagicMock(return_value="trace_fallback")
        with patch("pitwall.api.routes.openai.emit_inference_trace", mock_trace):
            response = await _post_chat(app_mod)

        assert response.status_code == 200
        assert response.json()["id"] == "chatcmpl-fallback"
        mock_trace.assert_called_once()
        call_kwargs = mock_trace.call_args.kwargs
        assert call_kwargs["status"] == "success"
        assert call_kwargs["provider_id"] == "prov_secondary"

        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_trace_emitted_with_error_on_all_providers_fail(self):
        """When all providers fail, trace is emitted with status='error'."""
        primary = _make_provider(
            id="prov_primary",
            endpoint_id="primary",
            priority=1,
            fallback_chain=["prov_secondary"],
        )
        secondary = _make_provider(
            id="prov_secondary",
            endpoint_id="secondary",
            priority=2,
        )
        app_mod = _setup_app_with_providers([primary, secondary])

        respx.post("https://api.runpod.ai/v2/primary/openai/v1/chat/completions").mock(
            return_value=httpx.Response(503, json={"error": "primary unavailable"})
        )

        respx.post("https://api.runpod.ai/v2/secondary/openai/v1/chat/completions").mock(
            return_value=httpx.Response(503, json={"error": "secondary unavailable"})
        )

        mock_trace = MagicMock(return_value="trace_error")
        with patch("pitwall.api.routes.openai.emit_inference_trace", mock_trace):
            response = await _post_chat(app_mod)

        assert response.status_code == 503
        mock_trace.assert_called_once()
        call_kwargs = mock_trace.call_args.kwargs
        assert call_kwargs["status"] == "error"

        app_mod.app.dependency_overrides.clear()


class TestStreamErrorPathTrace:
    """Verify emit_inference_trace is called correctly for stream error paths."""

    @respx.mock
    @pytest.mark.anyio
    async def test_trace_emitted_on_transport_failure(self):
        """When transport fails (connection error), trace is emitted with error status."""
        primary = _make_provider(
            id="prov_primary",
            endpoint_id="primary",
            priority=1,
            fallback_chain=["prov_secondary"],
        )
        secondary = _make_provider(
            id="prov_secondary",
            endpoint_id="secondary",
            priority=2,
        )
        app_mod = _setup_app_with_providers([primary, secondary])

        def primary_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dial failed", request=request)

        def secondary_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "chatcmpl-secondary"})

        respx.post("https://api.runpod.ai/v2/primary/openai/v1/chat/completions").mock(
            side_effect=primary_handler
        )

        respx.post("https://api.runpod.ai/v2/secondary/openai/v1/chat/completions").mock(
            side_effect=secondary_handler
        )

        mock_trace = MagicMock(return_value="trace_transport_err")
        with patch("pitwall.api.routes.openai.emit_inference_trace", mock_trace):
            response = await _post_chat(app_mod)

        assert response.status_code == 200
        assert response.json()["id"] == "chatcmpl-secondary"
        mock_trace.assert_called_once()
        call_kwargs = mock_trace.call_args.kwargs
        assert call_kwargs["status"] == "success"
        assert call_kwargs["provider_id"] == "prov_secondary"

        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_trace_includes_input_and_output_bytes(self):
        """Verify trace includes correct input/output byte counts."""
        provider = _make_provider(
            id="prov_primary",
            endpoint_id="primary",
            priority=1,
        )
        app_mod = _setup_app_with_providers([provider])

        upstream_url = "https://api.runpod.ai/v2/primary/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(
                200,
                json=_CHAT_COMPLETION_RESPONSE,
                headers={"content-length": "500"},
            )
        )

        mock_trace = MagicMock(return_value="trace_bytes")
        with patch("pitwall.api.routes.openai.emit_inference_trace", mock_trace):
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
                    headers={"Content-Type": "application/json"},
                )

        assert response.status_code == 200
        mock_trace.assert_called_once()
        call_kwargs = mock_trace.call_args.kwargs
        assert "input_bytes" in call_kwargs
        assert call_kwargs["input_bytes"] > 0
        assert "output_bytes" in call_kwargs

        app_mod.app.dependency_overrides.clear()

    @respx.mock
    @pytest.mark.anyio
    async def test_trace_includes_execution_time(self):
        """Verify trace includes execution time in milliseconds."""
        provider = _make_provider(
            id="prov_primary",
            endpoint_id="primary",
            priority=1,
        )
        app_mod = _setup_app_with_providers([provider])

        upstream_url = "https://api.runpod.ai/v2/primary/openai/v1/chat/completions"
        respx.post(upstream_url).mock(
            return_value=httpx.Response(200, json=_CHAT_COMPLETION_RESPONSE)
        )

        mock_trace = MagicMock(return_value="trace_time")
        with patch("pitwall.api.routes.openai.emit_inference_trace", mock_trace):
            response = await _post_chat(app_mod)

        assert response.status_code == 200
        mock_trace.assert_called_once()
        call_kwargs = mock_trace.call_args.kwargs
        assert "execution_ms" in call_kwargs
        assert call_kwargs["execution_ms"] > 0

        app_mod.app.dependency_overrides.clear()
