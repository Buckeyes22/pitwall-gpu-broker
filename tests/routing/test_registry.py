"""Registry tests for primary custom provider and public endpoint fallback registration.

Covers:
    - Primary custom vLLM provider (serverless_lb/serverless_queue) registration with fallback_chain
    - Public endpoint fallback registration with fallback_for
    - Routing plan verification for fallback chains

The "registry" is the provider registry: the system that stores and manages
provider registrations. These tests verify the end-to-end flow of registering
providers with fallback configurations and the routing planner's correct
handling of those configurations.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.routing import RoutingRequest, plan_route

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _capability(
    cap_id: str = "cap_llm_qwen3_32b",
    name: str = "llm.qwen3-32b",
) -> Capability:
    return Capability(
        id=cap_id,
        name=name,
        version="1.0.0",
        class_="llm",
        cost_mode="per_token",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _primary_custom_provider(
    provider_id: str = "prov_custom_qwen3_serverless",
    capability_id: str = "cap_llm_qwen3_32b",
    fallback_chain: list[str] | None = None,
    priority: int = 1,
) -> Provider:
    return Provider(
        id=provider_id,
        capability_id=capability_id,
        name=provider_id,
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id="qwen3-32b-awq",
        region="US-KS-2",
        config={
            "gpu_type": "NVIDIA L4",
            "lb_base_url": "https://qwen3-32b-awq.api.runpod.ai",
            "cost": {
                "mode": "per_second",
                "per_second_active": "0.001",
            },
            "fallback_chain": fallback_chain or [],
        },
        priority=priority,
        enabled=True,
        health_status="healthy",
        cold_start_p50_ms=2000,
        recent_error_rate=0.0,
        updated_at=_NOW,
    )


def _public_endpoint_fallback(
    provider_id: str = "prov_qwen3_32b_public",
    capability_id: str = "cap_llm_qwen3_32b",
    fallback_for: list[str] | None = None,
    priority: int = 2,
) -> Provider:
    return Provider(
        id=provider_id,
        capability_id=capability_id,
        name=provider_id,
        provider_type=ProviderType.PUBLIC_ENDPOINT,
        runpod_endpoint_id="qwen3-32b-awq",
        region="US-KS-2",
        config={
            "gpu_type": "NVIDIA L4",
            "openai_base_url": "https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1",
            "cost": {
                "mode": "per_token",
                "per_million_input_tokens": "0.30",
                "per_million_output_tokens": "0.60",
            },
            "fallback_for": fallback_for or [],
        },
        priority=priority,
        enabled=True,
        health_status="healthy",
        cold_start_p50_ms=0,
        recent_error_rate=0.0,
        updated_at=_NOW,
    )


class TestPrimaryCustomProviderRegistration:
    """Tests for primary custom provider registration."""

    def test_primary_custom_provider_with_empty_fallback_chain(self) -> None:
        """A primary custom provider can be registered with an empty fallback chain."""
        primary = _primary_custom_provider(fallback_chain=[])

        assert primary.provider_type == ProviderType.SERVERLESS_LB
        assert primary.priority == 1
        assert primary.config["fallback_chain"] == []

    def test_primary_custom_provider_with_explicit_fallback_chain(self) -> None:
        """A primary custom provider can specify an explicit fallback chain."""
        primary = _primary_custom_provider(fallback_chain=["prov_qwen3_32b_public"])

        assert primary.config["fallback_chain"] == ["prov_qwen3_32b_public"]

    def test_primary_custom_provider_priority_is_lower_than_fallback(self) -> None:
        """Primary providers should have priority 1, fallbacks priority 2+."""
        primary = _primary_custom_provider(priority=1)
        fallback = _public_endpoint_fallback(priority=2)

        assert primary.priority < fallback.priority


class TestPublicEndpointFallbackRegistration:
    """Tests for public endpoint fallback registration."""

    def test_public_endpoint_fallback_with_empty_fallback_for(self) -> None:
        """A public endpoint can be registered with an empty fallback_for list."""
        fallback = _public_endpoint_fallback(fallback_for=[])

        assert fallback.provider_type == ProviderType.PUBLIC_ENDPOINT
        assert fallback.config["fallback_for"] == []

    def test_public_endpoint_fallback_declares_primary(self) -> None:
        """A public endpoint fallback declares which primary it is a fallback for."""
        fallback = _public_endpoint_fallback(fallback_for=["prov_custom_qwen3_serverless"])

        assert fallback.config["fallback_for"] == ["prov_custom_qwen3_serverless"]

    def test_public_endpoint_has_zero_cold_start(self) -> None:
        """Public endpoints have 0 cold start since they are always-on."""
        fallback = _public_endpoint_fallback()

        assert fallback.cold_start_p50_ms == 0


class TestFallbackChainRouting:
    """Tests for routing with primary + fallback provider pairs."""

    def test_primary_selected_when_healthy(self) -> None:
        """The primary provider should be selected when it is healthy."""
        request = RoutingRequest(capability_name="llm.qwen3-32b")
        primary = _primary_custom_provider(fallback_chain=["prov_qwen3_32b_public"])
        fallback = _public_endpoint_fallback(fallback_for=["prov_custom_qwen3_serverless"])

        plan = plan_route(
            request,
            [primary, fallback],
            capability=_capability(),
            now=_NOW,
        )

        assert plan.selected_provider_id == "prov_custom_qwen3_serverless"
        assert plan.fallback_chain == (
            "prov_custom_qwen3_serverless",
            "prov_qwen3_32b_public",
        )

    def test_fallback_used_when_primary_unhealthy(self) -> None:
        """The fallback provider should be used when primary is unhealthy."""
        request = RoutingRequest(capability_name="llm.qwen3-32b")
        primary = _primary_custom_provider(fallback_chain=["prov_qwen3_32b_public"])
        primary_unhealthy = Provider(
            id=primary.id,
            capability_id=primary.capability_id,
            name=primary.name,
            provider_type=primary.provider_type,
            runpod_endpoint_id=primary.runpod_endpoint_id,
            region=primary.region,
            config=primary.config,
            priority=primary.priority,
            enabled=True,
            health_status="unhealthy",
            cold_start_p50_ms=primary.cold_start_p50_ms,
            recent_error_rate=primary.recent_error_rate,
            updated_at=_NOW,
        )
        fallback = _public_endpoint_fallback(fallback_for=["prov_custom_qwen3_serverless"])

        plan = plan_route(
            request,
            [primary_unhealthy, fallback],
            capability=_capability(),
            now=_NOW,
        )

        assert plan.selected_provider_id == "prov_qwen3_32b_public"
        assert plan.fallback_chain == ("prov_qwen3_32b_public",)

    def test_fallback_chain_order_from_explicit_config(self) -> None:
        """Fallback chain order should follow the explicit fallback_chain config."""
        request = RoutingRequest(capability_name="llm.qwen3-32b")
        primary = _primary_custom_provider(fallback_chain=["prov_fallback_a", "prov_fallback_b"])
        fallback_a = _public_endpoint_fallback(provider_id="prov_fallback_a")
        fallback_b = _public_endpoint_fallback(provider_id="prov_fallback_b")

        plan = plan_route(
            request,
            [primary, fallback_a, fallback_b],
            capability=_capability(),
            now=_NOW,
        )

        assert plan.fallback_chain == (
            "prov_custom_qwen3_serverless",
            "prov_fallback_a",
            "prov_fallback_b",
        )

    def test_fallback_chain_respects_max_attempts(self) -> None:
        """Fallback chain should be capped at max_attempts."""
        request = RoutingRequest(capability_name="llm.qwen3-32b")
        primary = _primary_custom_provider(
            fallback_chain=["prov_fallback_a", "prov_fallback_b", "prov_fallback_c"]
        )
        fallback_a = _public_endpoint_fallback(provider_id="prov_fallback_a")
        fallback_b = _public_endpoint_fallback(provider_id="prov_fallback_b")
        fallback_c = _public_endpoint_fallback(provider_id="prov_fallback_c")

        plan = plan_route(
            request,
            [primary, fallback_a, fallback_b, fallback_c],
            capability=_capability(),
            now=_NOW,
            max_attempts=2,
        )

        assert len(plan.attempts) == 2
        assert plan.attempts[0].provider_id == "prov_custom_qwen3_serverless"
        assert plan.attempts[1].provider_id == "prov_fallback_a"

    def test_fallback_provider_ids_from_primary_config(self) -> None:
        """fallback_provider_ids should be populated from primary's fallback_chain."""
        request = RoutingRequest(capability_name="llm.qwen3-32b")
        primary = _primary_custom_provider(fallback_chain=["prov_qwen3_32b_public"])
        fallback = _public_endpoint_fallback(fallback_for=["prov_custom_qwen3_serverless"])

        plan = plan_route(
            request,
            [primary, fallback],
            capability=_capability(),
            now=_NOW,
        )

        assert plan.fallback_provider_ids == ("prov_qwen3_32b_public",)

    def test_inferred_fallback_from_fallback_for(self) -> None:
        """Fallback candidates should be inferred from fallback_for config."""
        request = RoutingRequest(capability_name="llm.qwen3-32b")
        primary = _primary_custom_provider()
        fallback = _public_endpoint_fallback(fallback_for=["prov_custom_qwen3_serverless"])

        plan = plan_route(
            request,
            [primary, fallback],
            capability=_capability(),
            now=_NOW,
        )

        assert plan.fallback_chain == (
            "prov_custom_qwen3_serverless",
            "prov_qwen3_32b_public",
        )
        assert plan.fallback_provider_ids == ("prov_qwen3_32b_public",)


class TestProviderRegistryIntegration:
    """Integration tests for provider registry with routing."""

    def test_multiple_capabilities_with_separate_fallbacks(self) -> None:
        """Multiple capabilities can have their own separate fallback chains."""
        request_qwen = RoutingRequest(capability_name="llm.qwen3-32b")
        request_bge = RoutingRequest(capability_name="embedding.bge-m3")

        qwen_primary = _primary_custom_provider(
            provider_id="prov_qwen_primary",
            fallback_chain=["prov_qwen_public"],
        )
        qwen_fallback = _public_endpoint_fallback(
            provider_id="prov_qwen_public",
            fallback_for=["prov_qwen_primary"],
        )

        bge_capability = Capability(
            id="cap_embedding_bge_m3",
            name="embedding.bge-m3",
            version="1.0.0",
            class_="embedding",
            cost_mode="per_second",
            created_at=_NOW,
            updated_at=_NOW,
        )
        bge_primary = Provider(
            id="prov_bge_primary",
            capability_id="cap_embedding_bge_m3",
            name="prov_bge_primary",
            provider_type=ProviderType.SERVERLESS_QUEUE,
            runpod_endpoint_id="bge-m3-xyz",
            region="US-KS-2",
            config={
                "gpu_type": "NVIDIA L4",
                "cost": {"per_second_active": "0.001"},
                "fallback_chain": ["prov_bge_public"],
            },
            priority=1,
            enabled=True,
            health_status="healthy",
            cold_start_p50_ms=1000,
            recent_error_rate=0.0,
            updated_at=_NOW,
        )
        bge_fallback = Provider(
            id="prov_bge_public",
            capability_id="cap_embedding_bge_m3",
            name="prov_bge_public",
            provider_type=ProviderType.PUBLIC_ENDPOINT,
            runpod_endpoint_id="bge-m3-xyz",
            region="US-KS-2",
            config={
                "gpu_type": "NVIDIA L4",
                "openai_base_url": "https://api.runpod.ai/v2/bge-m3-xyz/openai/v1",
                "cost": {"per_token": {}},
                "fallback_for": ["prov_bge_primary"],
            },
            priority=2,
            enabled=True,
            health_status="healthy",
            cold_start_p50_ms=0,
            recent_error_rate=0.0,
            updated_at=_NOW,
        )

        qwen_plan = plan_route(
            request_qwen,
            [qwen_primary, qwen_fallback],
            capability=_capability(),
            now=_NOW,
        )
        bge_plan = plan_route(
            request_bge,
            [bge_primary, bge_fallback],
            capability=bge_capability,
            now=_NOW,
        )

        assert qwen_plan.selected_provider_id == "prov_qwen_primary"
        assert qwen_plan.fallback_provider_ids == ("prov_qwen_public",)
        assert bge_plan.selected_provider_id == "prov_bge_primary"
        assert bge_plan.fallback_provider_ids == ("prov_bge_public",)

    def test_fallback_chain_skips_nonexistent_providers(self) -> None:
        """Fallback chain should skip provider IDs that don't exist in the registry."""
        request = RoutingRequest(capability_name="llm.qwen3-32b")
        primary = _primary_custom_provider(
            fallback_chain=["prov_nonexistent", "prov_qwen_32b_public"]
        )
        fallback = _public_endpoint_fallback(
            provider_id="prov_qwen_32b_public",
            fallback_for=["prov_custom_qwen3_serverless"],
        )

        plan = plan_route(
            request,
            [primary, fallback],
            capability=_capability(),
            now=_NOW,
        )

        assert "prov_nonexistent" not in plan.fallback_chain
        assert plan.fallback_chain == (
            "prov_custom_qwen3_serverless",
            "prov_qwen_32b_public",
        )
