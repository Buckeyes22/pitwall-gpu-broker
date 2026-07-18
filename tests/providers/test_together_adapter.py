from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability
from pitwall.core.models import Provider as ProviderRecord
from pitwall.cost.estimator import PerTokenPricing
from pitwall.providers import (
    CredentialValidationError,
    Provider,
    ProviderOperationContext,
    ProvisionRequest,
    ReconcileRequest,
    StatusRequest,
    TeardownRequest,
    TogetherCredentials,
    TogetherProvider,
    TogetherProviderError,
    create_default_registry,
)


def _capability() -> Capability:
    now = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    return Capability(
        id="cap_llm_chat",
        name="llm.chat",
        version="1",
        class_=CapabilityClass.LLM,
        cost_mode=CostMode.PER_TOKEN,
        source=CapabilitySource.API,
        created_at=now,
        updated_at=now,
    )


def _provider_record(config: dict[str, Any] | None = None) -> ProviderRecord:
    return ProviderRecord(
        id="prov_together_llama",
        capability_id="cap_llm_chat",
        name="together-llama",
        provider_type=ProviderType.PUBLIC_ENDPOINT,
        config=config
        or {
            "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "cost": {
                "kind": "per_token",
                "per_million_input_tokens": "0.88",
                "per_million_output_tokens": "0.88",
            },
        },
        priority=1,
        source=CapabilitySource.API,
        updated_at=datetime(2026, 6, 2, 12, 0, tzinfo=UTC),
    )


def _credentials() -> TogetherCredentials:
    return TogetherCredentials(api_key="tog_test_key", base_url="https://api.together.test/v1")


def test_together_provider_satisfies_provider_protocol() -> None:
    provider = TogetherProvider()

    assert isinstance(provider, Provider)
    assert provider.id == "together"
    assert provider.name == "Together AI"


def test_default_registry_contains_together_provider() -> None:
    registry = create_default_registry()
    provider = registry.lookup("together")

    assert isinstance(provider, TogetherProvider)
    credentials = registry.validate_credentials(
        "together",
        {"api_key": "tog_test_key", "base_url": "https://api.together.test/v1"},
    )
    assert isinstance(credentials, TogetherCredentials)
    assert "tog_test_key" not in str(credentials.model_dump())


def test_together_credentials_reject_secret_bearing_base_url() -> None:
    registry = create_default_registry()

    with pytest.raises(CredentialValidationError) as raised:
        registry.validate_credentials(
            "together",
            {
                "api_key": "tog_super_secret",
                "base_url": "https://tog_super_secret@api.together.test/v1",
            },
        )

    assert raised.value.fields == ("base_url",)
    assert "tog_super_secret" not in str(raised.value)


def test_together_pricing_model_uses_per_token_max_tokens_upper_bound() -> None:
    provider = TogetherProvider()
    capability = _capability()
    provider_record = _provider_record(
        {
            "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "cost": {
                "kind": "per_token",
                "per_million_input_tokens": "1.00",
                "per_million_output_tokens": "2.00",
            },
        }
    )

    pricing = provider.pricing_model(capability, provider_record)

    assert isinstance(pricing, PerTokenPricing)
    payload = {"input_tokens": 100, "output_tokens": 10, "max_tokens": 1_000}
    assert pricing.estimate(capability, payload) == Decimal("0.000120")
    assert pricing.upper_bound(capability, payload) == Decimal("0.002100")


@pytest.mark.anyio
async def test_together_infer_posts_header_auth_without_secret_in_url() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == "https://api.together.test/v1/chat/completions"
        assert request.url.userinfo == b""
        assert request.headers["Authorization"] == "Bearer tog_test_key"
        assert request.headers["Content-Type"] == "application/json"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-123",
                "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                },
            },
        )

    provider = TogetherProvider(transport=httpx.MockTransport(handler))

    result = await provider.infer(
        credentials=_credentials(),
        provider_record=_provider_record(),
        payload={
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 32,
        },
    )

    assert len(requests) == 1
    body = requests[0].read().decode("utf-8")
    assert "tog_test_key" not in body
    assert "meta-llama/Llama-3.3-70B-Instruct-Turbo" in body
    assert result.content == "hello"
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 4
    assert result.total_tokens == 16
    assert result.finish_reason == "stop"


@pytest.mark.anyio
async def test_together_infer_rejects_payload_model_mismatch_before_network_call() -> None:
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(
            200,
            json={
                "model": "Qwen/Qwen3-235B-A22B-fp8-tput",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = TogetherProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="provider-priced model"):
        await provider.infer(
            credentials=_credentials(),
            provider_record=_provider_record(),
            payload={
                "model": "Qwen/Qwen3-235B-A22B-fp8-tput",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 8,
            },
        )

    assert called is False


@pytest.mark.anyio
async def test_together_infer_allows_payload_model_when_it_matches_provider_pricing() -> None:
    requests: list[httpx.Request] = []
    priced_model = "meta-llama/Llama-3.3-70B-Instruct-Turbo"

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": priced_model,
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = TogetherProvider(transport=httpx.MockTransport(handler))

    result = await provider.infer(
        credentials=_credentials(),
        provider_record=_provider_record(),
        payload={
            "model": priced_model,
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 8,
        },
    )

    assert priced_model in requests[0].read().decode("utf-8")
    assert result.model == priced_model


@pytest.mark.anyio
async def test_together_infer_requires_model_before_network_call() -> None:
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    provider = TogetherProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="model"):
        await provider.infer(
            credentials=_credentials(),
            provider_record=_provider_record(
                {"cost": {"per_million_input_tokens": "1.00", "per_million_output_tokens": "1.00"}}
            ),
            payload={"messages": [{"role": "user", "content": "hello"}]},
        )

    assert called is False


@pytest.mark.anyio
async def test_together_http_errors_do_not_leak_api_key() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "unauthorized"}})

    provider = TogetherProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(TogetherProviderError) as raised:
        await provider.infer(
            credentials=_credentials(),
            provider_record=_provider_record(),
            payload={"messages": [{"role": "user", "content": "hello"}], "max_tokens": 8},
        )

    assert raised.value.status_code == 401
    assert "tog_test_key" not in str(raised.value)


@pytest.mark.anyio
async def test_together_lifecycle_operations_are_not_lease_operations() -> None:
    provider = TogetherProvider()
    context = ProviderOperationContext(pool=object())
    credentials = _credentials()
    provider_record = _provider_record()

    with pytest.raises(NotImplementedError, match="inference-only"):
        await provider.provision(
            ProvisionRequest(
                context=context,
                capability=_capability(),
                provider_record=provider_record,
                credentials=credentials,
            )
        )

    with pytest.raises(NotImplementedError, match="inference-only"):
        await provider.status(
            StatusRequest(
                context=context,
                provider_record=provider_record,
                credentials=credentials,
                external_id="chatcmpl-123",
            )
        )

    with pytest.raises(NotImplementedError, match="inference-only"):
        await provider.reconcile(
            ReconcileRequest(
                context=context,
                provider_record=provider_record,
                credentials=credentials,
            )
        )

    with pytest.raises(NotImplementedError, match="inference-only"):
        await provider.teardown(
            TeardownRequest(
                context=context,
                provider_record=provider_record,
                credentials=credentials,
                lease_id="lease-123",
            )
        )
