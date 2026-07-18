"""OpenAI proxy fallback execution."""

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


def _make_capability() -> Capability:
    return Capability(
        id="cap_llm_qwen3_32b",
        name="llm.qwen3-32b",
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
        workload_id="wkl_openai_fallback_test",
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


@respx.mock
@pytest.mark.anyio
async def test_primary_503_falls_through_inside_five_seconds():
    primary = _make_provider(
        id="prov_primary",
        endpoint_id="primary",
        priority=1,
        fallback_chain=["prov_secondary"],
    )
    secondary = _make_provider(id="prov_secondary", endpoint_id="secondary", priority=2)
    app_mod = _setup_app_with_providers([primary, secondary])

    primary_call = respx.post("https://api.runpod.ai/v2/primary/openai/v1/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": "primary unavailable"})
    )
    secondary_call = respx.post(
        "https://api.runpod.ai/v2/secondary/openai/v1/chat/completions"
    ).mock(return_value=httpx.Response(200, json={"id": "chatcmpl-secondary"}))

    with patch("pitwall.api.routes.openai.emit_inference_trace"):
        response = await _post_chat(app_mod)

    assert response.status_code == 200
    assert response.json()["id"] == "chatcmpl-secondary"
    assert primary_call.called
    assert secondary_call.called

    app_mod.app.dependency_overrides.clear()


@respx.mock
@pytest.mark.anyio
async def test_primary_401_does_not_fallback():
    primary = _make_provider(
        id="prov_primary",
        endpoint_id="primary",
        priority=1,
        fallback_chain=["prov_secondary"],
    )
    secondary = _make_provider(id="prov_secondary", endpoint_id="secondary", priority=2)
    app_mod = _setup_app_with_providers([primary, secondary])

    primary_call = respx.post("https://api.runpod.ai/v2/primary/openai/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    secondary_call = respx.post(
        "https://api.runpod.ai/v2/secondary/openai/v1/chat/completions"
    ).mock(return_value=httpx.Response(200, json={"id": "should-not-run"}))

    with patch("pitwall.api.routes.openai.emit_inference_trace"):
        response = await _post_chat(app_mod)

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
    assert primary_call.called
    assert not secondary_call.called

    app_mod.app.dependency_overrides.clear()


@respx.mock
@pytest.mark.anyio
async def test_attempt_chain_capped_at_three():
    primary = _make_provider(
        id="prov_primary",
        endpoint_id="primary",
        priority=1,
        fallback_chain=["prov_second", "prov_third", "prov_fourth"],
    )
    second = _make_provider(id="prov_second", endpoint_id="second", priority=2)
    third = _make_provider(id="prov_third", endpoint_id="third", priority=3)
    fourth = _make_provider(id="prov_fourth", endpoint_id="fourth", priority=4)
    app_mod = _setup_app_with_providers([primary, second, third, fourth])

    primary_call = respx.post("https://api.runpod.ai/v2/primary/openai/v1/chat/completions").mock(
        return_value=httpx.Response(503, json={"provider": "primary"})
    )
    second_call = respx.post("https://api.runpod.ai/v2/second/openai/v1/chat/completions").mock(
        return_value=httpx.Response(503, json={"provider": "second"})
    )
    third_call = respx.post("https://api.runpod.ai/v2/third/openai/v1/chat/completions").mock(
        return_value=httpx.Response(503, json={"provider": "third"})
    )
    fourth_call = respx.post("https://api.runpod.ai/v2/fourth/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"provider": "fourth"})
    )

    with patch("pitwall.api.routes.openai.emit_inference_trace"):
        response = await _post_chat(app_mod)

    assert response.status_code == 503
    assert response.json()["provider"] == "third"
    assert primary_call.called
    assert second_call.called
    assert third_call.called
    assert not fourth_call.called

    app_mod.app.dependency_overrides.clear()


@respx.mock
@pytest.mark.anyio
async def test_transport_failure_retries_with_same_body():
    primary = _make_provider(
        id="prov_primary",
        endpoint_id="primary",
        priority=1,
        fallback_chain=["prov_secondary"],
    )
    secondary = _make_provider(id="prov_secondary", endpoint_id="secondary", priority=2)
    app_mod = _setup_app_with_providers([primary, secondary])
    seen_bodies: list[bytes] = []

    def primary_handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(request.content)
        raise httpx.ConnectError("dial failed", request=request)

    def secondary_handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(request.content)
        return httpx.Response(200, json={"id": "chatcmpl-secondary"})

    primary_call = respx.post("https://api.runpod.ai/v2/primary/openai/v1/chat/completions").mock(
        side_effect=primary_handler
    )
    secondary_call = respx.post(
        "https://api.runpod.ai/v2/secondary/openai/v1/chat/completions"
    ).mock(side_effect=secondary_handler)

    with patch("pitwall.api.routes.openai.emit_inference_trace"):
        response = await _post_chat(app_mod)

    assert response.status_code == 200
    assert response.json()["id"] == "chatcmpl-secondary"
    assert primary_call.called
    assert secondary_call.called
    assert len(seen_bodies) == 2
    assert seen_bodies[0] == seen_bodies[1]

    app_mod.app.dependency_overrides.clear()
