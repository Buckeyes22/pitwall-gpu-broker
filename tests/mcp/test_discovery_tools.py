"""Tests for MCP discovery tools.

These tests verify that the discovery tool handlers call the correct
repository methods and return properly structured JSON responses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pitwall.mcp.tools.discovery import (
    _capability_to_response,
    _provider_to_response,
    pitwall_describe_capability,
    pitwall_get_provider_health,
    pitwall_list_capabilities,
    pitwall_list_providers,
)

pytestmark = pytest.mark.anyio


class TestCapabilityToResponse:
    """Unit tests for the _capability_to_response helper."""

    def test_returns_dict(self) -> None:
        """Verify it returns a dict, not a model."""
        import datetime as dt

        from pitwall.core.models import Capability, CapabilityDefaults

        cap = Capability(
            id="cap_test",
            name="test.capability",
            version="1.0.0",
            class_="embedding",
            description="A test capability",
            input_schema={},
            output_schema={},
            defaults=CapabilityDefaults(),
            cost_mode="per_second",
            hints_supported=[],
            source="api",
            enabled=True,
            created_at=dt.datetime.now(dt.UTC),
            updated_at=dt.datetime.now(dt.UTC),
        )
        result = _capability_to_response(cap)
        assert isinstance(result, dict)
        assert result["id"] == "cap_test"
        assert result["name"] == "test.capability"


class TestProviderToResponse:
    """Unit tests for the _provider_to_response helper."""

    def test_returns_dict(self) -> None:
        """Verify it returns a dict, not a model."""
        import datetime as dt

        from pitwall.core.models import Provider

        prov = Provider(
            id="prov_test",
            capability_id="cap_test",
            name="test-provider",
            provider_type="serverless_lb",
            enabled=True,
            health_status="healthy",
            consecutive_failures=0,
            cooldown_trips=0,
            recent_error_rate=0.0,
            priority=1,
            updated_at=dt.datetime.now(dt.UTC),
        )
        result = _provider_to_response(prov)
        assert isinstance(result, dict)
        assert result["id"] == "prov_test"
        assert result["health_status"] == "healthy"


class TestPitwallListCapabilities:
    """Tests for pitwall_list_capabilities."""

    async def test_returns_capabilities_key(self) -> None:
        """Verify response has 'capabilities' key at top level."""
        with patch("pitwall.mcp.tools.discovery.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)

            result = await pitwall_list_capabilities()
            assert isinstance(result, dict)
            assert "capabilities" in result
            assert isinstance(result["capabilities"], list)

    async def test_passes_enabled_filter(self) -> None:
        """Verify enabled=True sets enabled_only on repo.list()."""
        with patch("pitwall.mcp.tools.discovery.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)

            await pitwall_list_capabilities(enabled=True)

            mock_conn.fetch.assert_called_once()
            call_args = mock_conn.fetch.call_args[0][0]
            assert "enabled = true" in call_args

    async def test_passes_class_filter(self) -> None:
        """Verify class filter is passed to repo.list()."""
        with patch("pitwall.mcp.tools.discovery.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)

            await pitwall_list_capabilities(capability_class="embedding")

            mock_conn.fetch.assert_called_once()
            call_args = mock_conn.fetch.call_args[0][0]
            assert "class" in call_args


class TestPitwallDescribeCapability:
    """Tests for pitwall_describe_capability."""

    async def test_raises_not_found_for_missing_capability(self) -> None:
        """Verify CapabilityNotFound is raised for unknown name."""
        from pitwall.api.exceptions import CapabilityNotFound

        with patch("pitwall.mcp.tools.discovery.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetchrow = AsyncMock(return_value=None)
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)

            with pytest.raises(CapabilityNotFound):
                await pitwall_describe_capability(name="nonexistent.capability")


class TestPitwallListProviders:
    """Tests for pitwall_list_providers."""

    async def test_returns_providers_key(self) -> None:
        """Verify response has 'providers' key at top level."""
        with patch("pitwall.mcp.tools.discovery.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)

            result = await pitwall_list_providers()
            assert isinstance(result, dict)
            assert "providers" in result
            assert isinstance(result["providers"], list)

    async def test_passes_capability_id_filter(self) -> None:
        """Verify capability_id filter is passed to repo.list()."""
        with patch("pitwall.mcp.tools.discovery.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)

            await pitwall_list_providers(capability_id="cap_test")

            mock_conn.fetch.assert_called_once()
            call_args = mock_conn.fetch.call_args[0][0]
            assert "capability_id" in call_args

    async def test_passes_provider_type_filter(self) -> None:
        """Verify provider_type filter is passed to repo.list()."""
        with patch("pitwall.mcp.tools.discovery.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)

            await pitwall_list_providers(provider_type="serverless_lb")

            mock_conn.fetch.assert_called_once()
            call_args = mock_conn.fetch.call_args[0][0]
            assert "provider_type" in call_args


class TestPitwallGetProviderHealth:
    """Tests for pitwall_get_provider_health."""

    async def test_raises_not_found_for_missing_provider(self) -> None:
        """Verify ProviderNotFound is raised for unknown ID."""
        from pitwall.api.exceptions import ProviderNotFound

        with patch("pitwall.mcp.tools.discovery.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetchrow = AsyncMock(return_value=None)
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)

            with pytest.raises(ProviderNotFound):
                await pitwall_get_provider_health(provider_id="nonexistent")

    async def test_returns_health_fields(self) -> None:
        """Verify response contains health-related fields."""
        import datetime as dt

        with patch("pitwall.mcp.tools.discovery.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetchrow = AsyncMock(
                return_value={
                    "id": "prov_test",
                    "capability_id": "cap_test",
                    "name": "test-provider",
                    "provider_type": "serverless_lb",
                    "runpod_endpoint_id": "abc123",
                    "runpod_template_id": None,
                    "region": "US-KS-2",
                    "cloud_type": None,
                    "config": {},
                    "priority": 1,
                    "enabled": True,
                    "health_status": "healthy",
                    "consecutive_failures": 0,
                    "cooldown_trips": 0,
                    "cold_start_p50_ms": 8000,
                    "cold_start_p95_ms": 22000,
                    "recent_error_rate": 0.0,
                    "cooldown_until": None,
                    "source": "api",
                    "last_applied_yaml_hash": None,
                    "updated_at": dt.datetime.now(dt.UTC),
                }
            )
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)

            result = await pitwall_get_provider_health(provider_id="prov_test")

            assert isinstance(result, dict)
            assert result["health_status"] == "healthy"
            assert "consecutive_failures" in result
            assert "cooldown_trips" in result
            assert "recent_error_rate" in result
