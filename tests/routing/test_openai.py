"""Tests for the OpenAI-compatible provider chain resolver."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pitwall.core.enums import ProviderType
from pitwall.core.models import Provider
from pitwall.routing.openai import (
    DEFAULT_OPENAI_MAX_ATTEMPTS,
    build_openai_url,
    openai_base_url_for_provider,
    ordered_openai_providers,
    resolve_openai_provider_chain,
    resolve_openai_provider_ids,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _provider(
    provider_id: str,
    *,
    name: str | None = None,
    provider_type: ProviderType = ProviderType.PUBLIC_ENDPOINT,
    priority: int = 1,
    runpod_endpoint_id: str = "qwen3-32b-awq",
    config: dict[str, object] | None = None,
    enabled: bool = True,
    health_status: str = "healthy",
    cooldown_until: datetime | None = None,
) -> Provider:
    return Provider(
        id=provider_id,
        capability_id="cap_llm_qwen3_32b",
        name=name or provider_id,
        provider_type=provider_type,
        runpod_endpoint_id=runpod_endpoint_id,
        config=config or {},
        priority=priority,
        enabled=enabled,
        health_status=health_status,
        cooldown_until=cooldown_until,
        updated_at=_NOW,
    )


def test_openai_chain_is_deterministic_by_priority_name_and_id_and_capped_at_three() -> None:
    providers = [
        _provider("prov_d", priority=4),
        _provider("prov_b", name="same", priority=2),
        _provider("prov_c", name="same", priority=2),
        _provider("prov_a", priority=1),
    ]

    chain = resolve_openai_provider_chain(providers, max_attempts=99)

    assert DEFAULT_OPENAI_MAX_ATTEMPTS == 3
    assert chain.provider_ids == ("prov_a", "prov_b", "prov_c")
    assert len(chain.attempts) == 3
    assert [attempt.attempt for attempt in chain.attempts] == [1, 2, 3]
    assert [attempt.backoff_before_attempt_s for attempt in chain.attempts] == [
        0.0,
        1.0,
        2.0,
    ]


def test_explicit_primary_fallback_chain_order_is_preserved_then_filled() -> None:
    providers = [
        _provider(
            "prov_primary",
            priority=1,
            config={"fallback_chain": ["prov_c", "prov_missing", "prov_b"]},
        ),
        _provider("prov_a", priority=2),
        _provider("prov_b", priority=3),
        _provider("prov_c", priority=4),
    ]

    chain = resolve_openai_provider_chain(providers)

    assert chain.provider_ids == ("prov_primary", "prov_c", "prov_b")
    assert chain.fallback_provider_ids == ("prov_c", "prov_b")


def test_primary_provider_id_overrides_priority_order() -> None:
    providers = [
        _provider("prov_default_primary", priority=1),
        _provider("prov_requested_primary", priority=5),
        _provider("prov_fallback", priority=6),
    ]

    chain = resolve_openai_provider_ids(
        providers,
        primary_provider_id="prov_requested_primary",
    )

    assert chain == (
        "prov_requested_primary",
        "prov_default_primary",
        "prov_fallback",
    )


def test_disabled_unhealthy_cooling_and_pod_lease_providers_are_not_attempted() -> None:
    providers = [
        _provider("prov_disabled", enabled=False, priority=1),
        _provider("prov_unhealthy", health_status="unhealthy", priority=2),
        _provider(
            "prov_cooling",
            priority=3,
            cooldown_until=_NOW + timedelta(minutes=5),
        ),
        _provider("prov_pod", provider_type=ProviderType.POD_LEASE, priority=4),
        _provider("prov_ready", priority=5),
    ]

    ordered = ordered_openai_providers(providers, now=_NOW)

    assert [provider.id for provider in ordered] == ["prov_ready"]


def test_openai_base_url_prefers_config_then_derives_from_provider_type() -> None:
    configured = _provider(
        "prov_configured",
        config={"openai_base_url": "https://example.test/openai/v1/"},
    )
    lb = _provider(
        "prov_lb",
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id="lb-endpoint",
    )
    public = _provider("prov_public", runpod_endpoint_id="public-endpoint")

    assert openai_base_url_for_provider(configured) == "https://example.test/openai/v1"
    assert openai_base_url_for_provider(lb) == "https://lb-endpoint.api.runpod.ai/openai/v1"
    assert openai_base_url_for_provider(public) == (
        "https://api.runpod.ai/v2/public-endpoint/openai/v1"
    )


def test_build_openai_url_keeps_relative_paths_on_base_host() -> None:
    assert (
        build_openai_url(
            "https://api.runpod.ai/v2/ep/openai/v1",
            "/v1/chat/completions?stream=true",
        )
        == "https://api.runpod.ai/v2/ep/openai/v1/chat/completions?stream=true"
    )


@pytest.mark.parametrize(
    "path",
    [
        "https://169.254.169.254/latest/meta-data",
        "//169.254.169.254/latest/meta-data",
        r"chat\completions",
        "../latest/meta-data",
        "chat/../latest/meta-data",
    ],
)
def test_build_openai_url_rejects_unsafe_proxy_paths(path: str) -> None:
    with pytest.raises(ValueError, match="OpenAI proxy path"):
        build_openai_url("https://api.runpod.ai/v2/ep/openai/v1", path)


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        resolve_openai_provider_chain([_provider("prov_a")], max_attempts=0)
