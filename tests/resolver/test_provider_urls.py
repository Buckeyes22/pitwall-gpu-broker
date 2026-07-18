"""Tests for pitwall.resolver.provider_urls — .

Covers every provider-type URL resolution path, boundary cases (missing
endpoint_id, wrong type), and the convenience ``provider_url`` dispatcher.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pitwall.core.enums import ProviderType
from pitwall.core.models import Provider
from pitwall.resolver.provider_urls import (
    lb_url,
    openai_base_url,
    provider_url,
    public_endpoint_url,
    queue_url,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _provider(
    provider_type: ProviderType = ProviderType.SERVERLESS_QUEUE,
    endpoint_id: str | None = "abc123",
    **overrides: object,
) -> Provider:
    kw: dict[str, object] = {
        "id": "prov_test",
        "capability_id": "cap_test",
        "name": "test-provider",
        "provider_type": provider_type,
        "runpod_endpoint_id": endpoint_id,
        "priority": 0,
        "updated_at": _NOW,
    }
    kw.update(overrides)
    return Provider(**kw)


class TestQueueUrl:
    def test_base_url(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "abc123")
        assert queue_url(p) == "https://api.runpod.ai/v2/abc123"

    def test_with_path(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "abc123")
        assert queue_url(p, "/runsync") == "https://api.runpod.ai/v2/abc123/runsync"

    def test_with_health_path(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "abc123")
        assert queue_url(p, "/health") == "https://api.runpod.ai/v2/abc123/health"

    def test_public_endpoint_uses_queue_surface(self) -> None:
        p = _provider(ProviderType.PUBLIC_ENDPOINT, "qwen3-32b-awq")
        assert queue_url(p, "/runsync") == ("https://api.runpod.ai/v2/qwen3-32b-awq/runsync")

    def test_lb_type_rejected(self) -> None:
        p = _provider(ProviderType.SERVERLESS_LB, "abc123")
        with pytest.raises(ValueError, match="not applicable"):
            queue_url(p)

    def test_pod_lease_rejected(self) -> None:
        p = _provider(ProviderType.POD_LEASE, None)
        with pytest.raises(ValueError):
            queue_url(p)

    def test_missing_endpoint_id(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, None)
        with pytest.raises(ValueError, match="no runpod_endpoint_id"):
            queue_url(p)


class TestLbUrl:
    def test_base_url(self) -> None:
        p = _provider(ProviderType.SERVERLESS_LB, "eptest00000000")
        assert lb_url(p) == "https://eptest00000000.api.runpod.ai/"

    def test_embed_path(self) -> None:
        p = _provider(ProviderType.SERVERLESS_LB, "eptest00000000")
        assert lb_url(p, "/embed") == "https://eptest00000000.api.runpod.ai/embed"

    def test_ping_path(self) -> None:
        p = _provider(ProviderType.SERVERLESS_LB, "eptest00000000")
        assert lb_url(p, "/ping") == "https://eptest00000000.api.runpod.ai/ping"

    def test_path_without_leading_slash(self) -> None:
        p = _provider(ProviderType.SERVERLESS_LB, "eptest00000000")
        assert lb_url(p, "embed") == "https://eptest00000000.api.runpod.ai/embed"

    def test_queue_type_rejected(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "abc123")
        with pytest.raises(ValueError, match="not applicable"):
            lb_url(p)

    def test_missing_endpoint_id(self) -> None:
        p = _provider(ProviderType.SERVERLESS_LB, None)
        with pytest.raises(ValueError, match="no runpod_endpoint_id"):
            lb_url(p)


class TestOpenaiBaseUrl:
    def test_queue_provider(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "eptest00000002")
        assert openai_base_url(p) == ("https://api.runpod.ai/v2/eptest00000002/openai/v1")

    def test_public_endpoint(self) -> None:
        p = _provider(ProviderType.PUBLIC_ENDPOINT, "qwen3-32b-awq")
        assert openai_base_url(p) == ("https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1")

    def test_lb_provider(self) -> None:
        p = _provider(ProviderType.SERVERLESS_LB, "eptest00000000")
        assert openai_base_url(p) == ("https://eptest00000000.api.runpod.ai/openai/v1")

    def test_pod_lease_rejected(self) -> None:
        p = _provider(ProviderType.POD_LEASE, None)
        with pytest.raises(ValueError, match="pod_lease"):
            openai_base_url(p)

    def test_missing_endpoint_id(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, None)
        with pytest.raises(ValueError, match="no runpod_endpoint_id"):
            openai_base_url(p)


class TestPublicEndpointUrl:
    def test_public_endpoint(self) -> None:
        p = _provider(ProviderType.PUBLIC_ENDPOINT, "qwen3-32b-awq")
        assert public_endpoint_url(p) == ("https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1")

    def test_granite_example(self) -> None:
        p = _provider(ProviderType.PUBLIC_ENDPOINT, "granite-4-0-h-small")
        assert public_endpoint_url(p) == ("https://api.runpod.ai/v2/granite-4-0-h-small/openai/v1")

    def test_queue_type_rejected(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "abc123")
        with pytest.raises(ValueError, match="not applicable"):
            public_endpoint_url(p)

    def test_lb_type_rejected(self) -> None:
        p = _provider(ProviderType.SERVERLESS_LB, "abc123")
        with pytest.raises(ValueError, match="not applicable"):
            public_endpoint_url(p)

    def test_missing_endpoint_id(self) -> None:
        p = _provider(ProviderType.PUBLIC_ENDPOINT, None)
        with pytest.raises(ValueError, match="no runpod_endpoint_id"):
            public_endpoint_url(p)


class TestProviderUrl:
    def test_serverless_queue(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "abc123")
        assert provider_url(p) == "https://api.runpod.ai/v2/abc123"

    def test_serverless_lb(self) -> None:
        p = _provider(ProviderType.SERVERLESS_LB, "eptest00000000")
        assert provider_url(p) == "https://eptest00000000.api.runpod.ai/"

    def test_public_endpoint(self) -> None:
        p = _provider(ProviderType.PUBLIC_ENDPOINT, "qwen3-32b-awq")
        assert provider_url(p) == "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1"

    def test_pod_lease_rejected(self) -> None:
        p = _provider(ProviderType.POD_LEASE, None)
        with pytest.raises(ValueError, match="pod_lease"):
            provider_url(p)

    def test_missing_endpoint_id(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, None)
        with pytest.raises(ValueError, match="no runpod_endpoint_id"):
            provider_url(p)


class TestSpecExamples:
    """Verify the exact URLs from the v0.3 spec examples."""

    def test_bge_m3_lb_embed(self) -> None:
        p = _provider(ProviderType.SERVERLESS_LB, "eptest00000000")
        assert lb_url(p, "/embed") == "https://eptest00000000.api.runpod.ai/embed"

    def test_qwen3_32b_public_openai(self) -> None:
        p = _provider(ProviderType.PUBLIC_ENDPOINT, "qwen3-32b-awq")
        assert openai_base_url(p) == ("https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1")

    def test_custom_text_endpoint_openai(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "eptest00000002")
        assert openai_base_url(p) == ("https://api.runpod.ai/v2/eptest00000002/openai/v1")

    def test_custom_vlm_endpoint_openai(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "eptest00000001")
        assert openai_base_url(p) == ("https://api.runpod.ai/v2/eptest00000001/openai/v1")

    def test_bge_m3_lb_health(self) -> None:
        p = _provider(ProviderType.SERVERLESS_LB, "eptest00000000")
        assert lb_url(p, "/ping") == "https://eptest00000000.api.runpod.ai/ping"

    def test_queue_sync_run(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "abc123")
        assert queue_url(p, "/runsync") == "https://api.runpod.ai/v2/abc123/runsync"

    def test_queue_async_run(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "abc123")
        assert queue_url(p, "/run") == "https://api.runpod.ai/v2/abc123/run"


class TestChatCompletionsUrl:
    """URL-composition assertions for the ServerlessSettings pattern.

    The chat_completions_url pattern returns
    ``https://api.runpod.ai/v2/{endpoint_id}/openai/v1/chat/completions``.
    In Pitwall this is ``openai_base_url(provider) + "/chat/completions"``.
    These tests assert the composition pattern holds for all provider types
    that expose an OpenAI-compatible endpoint.
    """

    def test_vlm_endpoint_chat_url(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "abc")
        assert f"{openai_base_url(p)}/chat/completions" == (
            "https://api.runpod.ai/v2/abc/openai/v1/chat/completions"
        )

    def test_text_endpoint_chat_url(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "def")
        assert f"{openai_base_url(p)}/chat/completions" == (
            "https://api.runpod.ai/v2/def/openai/v1/chat/completions"
        )

    def test_public_endpoint_chat_url(self) -> None:
        p = _provider(ProviderType.PUBLIC_ENDPOINT, "qwen3-32b-awq")
        assert f"{openai_base_url(p)}/chat/completions" == (
            "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1/chat/completions"
        )

    def test_lb_endpoint_chat_url(self) -> None:
        p = _provider(ProviderType.SERVERLESS_LB, "eptest00000000")
        assert f"{openai_base_url(p)}/chat/completions" == (
            "https://eptest00000000.api.runpod.ai/openai/v1/chat/completions"
        )

    def test_different_endpoints_produce_different_urls(self) -> None:
        vlm_provider = _provider(ProviderType.SERVERLESS_QUEUE, "vlm-ep")
        text_provider = _provider(ProviderType.SERVERLESS_QUEUE, "text-ep")
        vlm_chat = f"{openai_base_url(vlm_provider)}/chat/completions"
        text_chat = f"{openai_base_url(text_provider)}/chat/completions"
        assert vlm_chat != text_chat
        assert "vlm-ep" in vlm_chat
        assert "text-ep" in text_chat

    def test_chat_url_path_is_correct(self) -> None:
        p = _provider(ProviderType.SERVERLESS_QUEUE, "abc123")
        chat_url = f"{openai_base_url(p)}/chat/completions"
        assert chat_url.endswith("/chat/completions")
