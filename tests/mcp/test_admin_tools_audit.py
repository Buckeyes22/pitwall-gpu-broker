"""Mocked audit tests for MCP admin tools.

These tests exercise the audit paths for RunPod provider and capability
management without live spend. Each test verifies that insert_audit is called
with the correct parameters when admin operations are performed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pitwall.core.enums import (
    CapabilityClass,
    CapabilitySource,
    CostMode,
    ProviderType,
)
from pitwall.core.models import Capability, Provider
from pitwall.mcp.tools.admin import (
    pitwall_create_capability,
    pitwall_create_provider,
    pitwall_disable_provider,
    pitwall_hibernate_provider,
    pitwall_update_capability,
    pitwall_update_provider,
)

pytestmark = pytest.mark.anyio


TEST_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _make_mock_pool() -> MagicMock:
    """Build a mock asyncpg pool with async context manager support."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="SELECT 1")
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    acquire_context = MagicMock()
    acquire_context.__aenter__ = AsyncMock(return_value=conn)
    acquire_context.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_context)
    return pool


class TestCreateCapabilityAudit:
    """Tests for audit trail on pitwall_create_capability."""

    async def test_insert_audit_called_with_create_action(self) -> None:
        pool = _make_mock_pool()
        conn = pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(return_value=None)

        created_cap = Capability(
            id="cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
            name="embedding.test",
            version="1.0.0",
            class_=CapabilityClass.EMBEDDING,
            cost_mode=CostMode.PER_REQUEST,
            enabled=True,
            hints_supported=[],
            source=CapabilitySource.API,
            created_at=TEST_NOW,
            updated_at=TEST_NOW,
        )
        with (
            patch("pitwall.mcp.tools.admin.get_pool", AsyncMock(return_value=pool)),
            patch("pitwall.mcp.tools.admin.insert_audit", new_callable=AsyncMock) as mock_insert,
        ):
            with patch(
                "pitwall.mcp.tools.admin.ulid_new", return_value="01HQXR8K9N3JZQP7VW4MEX2YBA"
            ):
                mock_repo = MagicMock()
                mock_repo.get_by_name = AsyncMock(return_value=None)
                mock_repo.create = AsyncMock(return_value=created_cap)
                with patch(
                    "pitwall.mcp.tools.admin.CapabilityRepository",
                    return_value=mock_repo,
                ):
                    await pitwall_create_capability(
                        name="embedding.test",
                        version="1.0.0",
                        capability_class="embedding",
                        cost_mode="per_request",
                    )

            mock_insert.assert_called_once()
            call_kwargs = mock_insert.call_args.kwargs
            assert call_kwargs["actor"] == "mcp:admin"
            assert call_kwargs["action"] == "create"
            assert call_kwargs["entity_type"] == "capability"
            assert call_kwargs["entity_id"] == "cap_01HQXR8K9N3JZQP7VW4MEX2YBA"
            assert call_kwargs["new_value"] == {
                "name": "embedding.test",
                "version": "1.0.0",
            }

    async def test_insert_audit_not_called_on_conflict(self) -> None:
        # Resolve at call time: api-suite fixtures reload pitwall.api modules,
        # and the tool under test late-imports this class the same way.
        from pitwall.api.exceptions import CapabilityConflict

        pool = _make_mock_pool()
        existing_cap = Capability(
            id="cap_existing",
            name="embedding.test",
            version="1.0.0",
            class_=CapabilityClass.EMBEDDING,
            cost_mode=CostMode.PER_REQUEST,
            enabled=True,
            hints_supported=[],
            source=CapabilitySource.API,
            created_at=TEST_NOW,
            updated_at=TEST_NOW,
        )
        with (
            patch("pitwall.mcp.tools.admin.get_pool", AsyncMock(return_value=pool)),
            patch("pitwall.mcp.tools.admin.insert_audit", new_callable=AsyncMock) as mock_insert,
        ):
            mock_repo = MagicMock()
            mock_repo.get_by_name = AsyncMock(return_value=existing_cap)
            with (
                patch(
                    "pitwall.mcp.tools.admin.CapabilityRepository",
                    return_value=mock_repo,
                ),
                pytest.raises(CapabilityConflict),
            ):
                await pitwall_create_capability(
                    name="embedding.test",
                    version="1.0.0",
                    capability_class="embedding",
                    cost_mode="per_request",
                )

            mock_insert.assert_not_called()


class TestUpdateCapabilityAudit:
    """Tests for audit trail on pitwall_update_capability."""

    async def test_insert_audit_called_with_update_action(self) -> None:
        pool = _make_mock_pool()
        existing_cap = Capability(
            id="cap_test_001",
            name="embedding.test",
            version="1.0.0",
            class_=CapabilityClass.EMBEDDING,
            cost_mode=CostMode.PER_REQUEST,
            enabled=True,
            description="original description",
            input_schema={},
            output_schema={},
            hints_supported=[],
            source=CapabilitySource.API,
            created_at=TEST_NOW,
            updated_at=TEST_NOW,
        )
        updated_cap = Capability(
            id="cap_test_001",
            name="embedding.test",
            version="1.0.0",
            class_=CapabilityClass.EMBEDDING,
            cost_mode=CostMode.PER_REQUEST,
            enabled=True,
            description="updated description",
            input_schema={},
            output_schema={},
            hints_supported=[],
            source=CapabilitySource.API,
            created_at=TEST_NOW,
            updated_at=TEST_NOW,
        )
        with (
            patch("pitwall.mcp.tools.admin.get_pool", AsyncMock(return_value=pool)),
            patch("pitwall.mcp.tools.admin.insert_audit", new_callable=AsyncMock) as mock_insert,
        ):
            mock_repo = MagicMock()
            mock_repo.get = AsyncMock(return_value=existing_cap)
            mock_repo.patch = AsyncMock(return_value=updated_cap)
            with patch(
                "pitwall.mcp.tools.admin.CapabilityRepository",
                return_value=mock_repo,
            ):
                await pitwall_update_capability(
                    capability_id="cap_test_001",
                    description="updated description",
                )

            mock_insert.assert_called_once()
            call_kwargs = mock_insert.call_args.kwargs
            assert call_kwargs["actor"] == "mcp:admin"
            assert call_kwargs["action"] == "update"
            assert call_kwargs["entity_type"] == "capability"
            assert call_kwargs["entity_id"] == "cap_test_001"
            assert call_kwargs["old_value"]["description"] == "original description"
            assert call_kwargs["new_value"]["description"] == "updated description"


class TestCreateProviderAudit:
    """Tests for audit trail on pitwall_create_provider."""

    async def test_insert_audit_called_with_create_action(self) -> None:
        pool = _make_mock_pool()
        conn = pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(return_value=None)

        mock_provider = Provider(
            id="prov_test_001",
            capability_id="cap_embedding_bge_m3",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )
        with (
            patch("pitwall.mcp.tools.admin.get_pool", AsyncMock(return_value=pool)),
            patch("pitwall.mcp.tools.admin.insert_audit", new_callable=AsyncMock) as mock_insert,
        ):
            mock_repo = MagicMock()
            mock_repo.get_by_name = AsyncMock(return_value=None)
            mock_repo.create = AsyncMock(return_value=mock_provider)
            with patch(
                "pitwall.mcp.tools.admin.ProviderRepository",
                return_value=mock_repo,
            ):
                await pitwall_create_provider(
                    capability_id="cap_embedding_bge_m3",
                    name="test-provider",
                    provider_type="serverless_lb",
                    runpod_endpoint_id="ep_test",
                )

            mock_insert.assert_called_once()
            call_kwargs = mock_insert.call_args.kwargs
            assert call_kwargs["actor"] == "mcp:admin"
            assert call_kwargs["action"] == "create"
            assert call_kwargs["entity_type"] == "provider"
            assert call_kwargs["entity_id"] == "prov_test_001"
            assert call_kwargs["new_value"] == {
                "name": "test-provider",
                "capability_id": "cap_embedding_bge_m3",
            }


class TestUpdateProviderAudit:
    """Tests for audit trail on pitwall_update_provider."""

    async def test_insert_audit_called_with_update_action(self) -> None:
        pool = _make_mock_pool()
        existing_provider = Provider(
            id="prov_test_001",
            capability_id="cap_embedding_bge_m3",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )
        updated_provider = Provider(
            id="prov_test_001",
            capability_id="cap_embedding_bge_m3",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=2,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )
        with (
            patch("pitwall.mcp.tools.admin.get_pool", AsyncMock(return_value=pool)),
            patch("pitwall.mcp.tools.admin.insert_audit", new_callable=AsyncMock) as mock_insert,
        ):
            mock_repo = MagicMock()
            mock_repo.get = AsyncMock(return_value=existing_provider)
            mock_repo.patch = AsyncMock(return_value=updated_provider)
            with patch(
                "pitwall.mcp.tools.admin.ProviderRepository",
                return_value=mock_repo,
            ):
                await pitwall_update_provider(
                    provider_id="prov_test_001",
                    priority=2,
                )

            mock_insert.assert_called_once()
            call_kwargs = mock_insert.call_args.kwargs
            assert call_kwargs["actor"] == "mcp:admin"
            assert call_kwargs["action"] == "update"
            assert call_kwargs["entity_type"] == "provider"
            assert call_kwargs["entity_id"] == "prov_test_001"
            assert call_kwargs["old_value"]["priority"] == 1
            assert call_kwargs["new_value"]["priority"] == 2


class TestDisableProviderAudit:
    """Tests for audit trail on pitwall_disable_provider."""

    async def test_insert_audit_called_with_disable_action(self) -> None:
        pool = _make_mock_pool()
        existing_provider = Provider(
            id="prov_test_001",
            capability_id="cap_embedding_bge_m3",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )
        disabled_provider = Provider(
            id="prov_test_001",
            capability_id="cap_embedding_bge_m3",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=False,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )
        with (
            patch("pitwall.mcp.tools.admin.get_pool", AsyncMock(return_value=pool)),
            patch("pitwall.mcp.tools.admin.insert_audit", new_callable=AsyncMock) as mock_insert,
        ):
            mock_repo = MagicMock()
            mock_repo.get = AsyncMock(return_value=existing_provider)
            mock_repo.disable = AsyncMock(return_value=disabled_provider)
            with patch(
                "pitwall.mcp.tools.admin.ProviderRepository",
                return_value=mock_repo,
            ):
                await pitwall_disable_provider(provider_id="prov_test_001")

            mock_insert.assert_called_once()
            call_kwargs = mock_insert.call_args.kwargs
            assert call_kwargs["actor"] == "mcp:admin"
            assert call_kwargs["action"] == "disable"
            assert call_kwargs["entity_type"] == "provider"
            assert call_kwargs["entity_id"] == "prov_test_001"
            assert call_kwargs["old_value"] == {"enabled": True}
            assert call_kwargs["new_value"] == {"enabled": False}


class TestHibernateProviderAudit:
    """Tests for audit trail on pitwall_hibernate_provider."""

    async def test_insert_audit_called_with_hibernate_action(self) -> None:
        pool = _make_mock_pool()
        existing_provider = Provider(
            id="prov_test_001",
            capability_id="cap_embedding_bge_m3",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )
        hibernated_provider = Provider(
            id="prov_test_001",
            capability_id="cap_embedding_bge_m3",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="hibernated",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )
        with (
            patch("pitwall.mcp.tools.admin.get_pool", AsyncMock(return_value=pool)),
            patch("pitwall.mcp.tools.admin.insert_audit", new_callable=AsyncMock) as mock_insert,
        ):
            mock_repo = MagicMock()
            mock_repo.get = AsyncMock(return_value=existing_provider)
            mock_repo.patch = AsyncMock(return_value=hibernated_provider)
            with patch(
                "pitwall.mcp.tools.admin.ProviderRepository",
                return_value=mock_repo,
            ):
                await pitwall_hibernate_provider(provider_id="prov_test_001")

            mock_insert.assert_called_once()
            call_kwargs = mock_insert.call_args.kwargs
            assert call_kwargs["actor"] == "mcp:admin"
            assert call_kwargs["action"] == "hibernate"
            assert call_kwargs["entity_type"] == "provider"
            assert call_kwargs["entity_id"] == "prov_test_001"
            assert call_kwargs["old_value"] == {"health_status": "healthy"}
            assert call_kwargs["new_value"] == {"health_status": "hibernated"}


class TestAuditActorIsMcpAdmin:
    """Verify all admin tool audit entries use actor='mcp:admin'."""

    async def test_actor_is_mcp_admin_create_provider(self) -> None:
        pool = _make_mock_pool()
        mock_provider = Provider(
            id="prov_test",
            capability_id="cap_test",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )
        existing_provider = Provider(
            id="prov_test",
            capability_id="cap_test",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )

        with (
            patch("pitwall.mcp.tools.admin.get_pool", AsyncMock(return_value=pool)),
            patch("pitwall.mcp.tools.admin.insert_audit", new_callable=AsyncMock) as mock_insert,
        ):
            mock_repo = MagicMock()
            mock_repo.get_by_name = AsyncMock(return_value=None)
            mock_repo.get = AsyncMock(return_value=existing_provider)
            mock_repo.create = AsyncMock(return_value=mock_provider)
            mock_repo.patch = AsyncMock(return_value=mock_provider)
            mock_repo.disable = AsyncMock(return_value=mock_provider)
            with patch(
                "pitwall.mcp.tools.admin.ProviderRepository",
                return_value=mock_repo,
            ):
                await pitwall_create_provider(
                    capability_id="cap_test",
                    name="test-provider",
                    provider_type="serverless_lb",
                )

            assert mock_insert.call_count == 1
            assert mock_insert.call_args.kwargs["actor"] == "mcp:admin"

    async def test_actor_is_mcp_admin_update_provider(self) -> None:
        pool = _make_mock_pool()
        mock_provider = Provider(
            id="prov_test",
            capability_id="cap_test",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )
        existing_provider = Provider(
            id="prov_test",
            capability_id="cap_test",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )

        with (
            patch("pitwall.mcp.tools.admin.get_pool", AsyncMock(return_value=pool)),
            patch("pitwall.mcp.tools.admin.insert_audit", new_callable=AsyncMock) as mock_insert,
        ):
            mock_repo = MagicMock()
            mock_repo.get = AsyncMock(return_value=existing_provider)
            mock_repo.patch = AsyncMock(return_value=mock_provider)
            with patch(
                "pitwall.mcp.tools.admin.ProviderRepository",
                return_value=mock_repo,
            ):
                await pitwall_update_provider(provider_id="prov_test", priority=5)

            assert mock_insert.call_count == 1
            assert mock_insert.call_args.kwargs["actor"] == "mcp:admin"

    async def test_actor_is_mcp_admin_disable_provider(self) -> None:
        pool = _make_mock_pool()
        mock_provider = Provider(
            id="prov_test",
            capability_id="cap_test",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )
        existing_provider = Provider(
            id="prov_test",
            capability_id="cap_test",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )

        with (
            patch("pitwall.mcp.tools.admin.get_pool", AsyncMock(return_value=pool)),
            patch("pitwall.mcp.tools.admin.insert_audit", new_callable=AsyncMock) as mock_insert,
        ):
            mock_repo = MagicMock()
            mock_repo.get = AsyncMock(return_value=existing_provider)
            mock_repo.disable = AsyncMock(return_value=mock_provider)
            with patch(
                "pitwall.mcp.tools.admin.ProviderRepository",
                return_value=mock_repo,
            ):
                await pitwall_disable_provider(provider_id="prov_test")

            assert mock_insert.call_count == 1
            assert mock_insert.call_args.kwargs["actor"] == "mcp:admin"

    async def test_actor_is_mcp_admin_hibernate_provider(self) -> None:
        pool = _make_mock_pool()
        mock_provider = Provider(
            id="prov_test",
            capability_id="cap_test",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )
        existing_provider = Provider(
            id="prov_test",
            capability_id="cap_test",
            name="test-provider",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="ep_test",
            enabled=True,
            health_status="healthy",
            priority=1,
            source=CapabilitySource.API,
            updated_at=TEST_NOW,
        )

        with (
            patch("pitwall.mcp.tools.admin.get_pool", AsyncMock(return_value=pool)),
            patch("pitwall.mcp.tools.admin.insert_audit", new_callable=AsyncMock) as mock_insert,
        ):
            mock_repo = MagicMock()
            mock_repo.get = AsyncMock(return_value=existing_provider)
            mock_repo.patch = AsyncMock(return_value=mock_provider)
            with patch(
                "pitwall.mcp.tools.admin.ProviderRepository",
                return_value=mock_repo,
            ):
                await pitwall_hibernate_provider(provider_id="prov_test")

            assert mock_insert.call_count == 1
            assert mock_insert.call_args.kwargs["actor"] == "mcp:admin"
