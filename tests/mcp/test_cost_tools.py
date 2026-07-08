"""Tests for MCP cost and workload history tools.

These tests verify that the cost tool handlers call the correct
service-layer functions and return properly structured JSON responses
with numeric cost fields (not strings).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pitwall.mcp.tools.cost import (
    pitwall_cost_summary,
    pitwall_recent_workloads,
)

pytestmark = pytest.mark.anyio


class TestPitwallCostSummary:
    """Tests for pitwall_cost_summary."""

    async def test_returns_total_usd_and_entries_keys(self) -> None:
        """Verify response has 'total_usd' and 'entries' keys at top level."""
        with patch("pitwall.mcp.tools.cost.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_conn.fetchrow = AsyncMock(return_value=(Decimal("0"),))
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await pitwall_cost_summary()

            assert isinstance(result, dict)
            assert "total_usd" in result
            assert "entries" in result
            assert isinstance(result["entries"], list)

    async def test_total_usd_is_numeric(self) -> None:
        """Verify total_usd is returned as a JSON number (float), not a string."""
        with patch("pitwall.mcp.tools.cost.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_conn.fetchrow = AsyncMock(return_value=(Decimal("123.456789"),))
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await pitwall_cost_summary()

            assert isinstance(result["total_usd"], float)
            assert result["total_usd"] == 123.456789

    async def test_passes_capability_class_filter(self) -> None:
        """Verify capability_class filter is passed to the query."""
        with patch("pitwall.mcp.tools.cost.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_conn.fetchrow = AsyncMock(return_value=(Decimal("0"),))
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

            await pitwall_cost_summary(capability_class="embedding")

            mock_conn.fetch.assert_called_once()
            call_args = mock_conn.fetch.call_args[0]
            assert "capability_class = $1" in call_args[0]
            assert "embedding" in call_args[1:]

    async def test_passes_date_range_filters(self) -> None:
        """Verify since and until date filters are passed to the query."""
        with patch("pitwall.mcp.tools.cost.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_conn.fetchrow = AsyncMock(return_value=(Decimal("0"),))
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

            await pitwall_cost_summary(since="2026-01-01", until="2026-01-31")

            mock_conn.fetch.assert_called_once()
            call_args = mock_conn.fetch.call_args[0]
            query = call_args[0]
            assert "day >= $" in query
            assert "day <= $" in query

    async def test_entries_have_numeric_cost_usd(self) -> None:
        """Verify entry cost_usd is returned as a JSON number (float), not a string."""
        with patch("pitwall.mcp.tools.cost.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_row = MagicMock()
            mock_row.__getitem__ = MagicMock(
                side_effect=lambda k: {
                    "day": date(2026, 1, 15),
                    "capability_class": "embedding",
                    "provider_type": "serverless_lb",
                    "workload_count": 10,
                    "cost_usd": Decimal("50.123456"),
                }[k]
            )

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[mock_row])
            mock_conn.fetchrow = AsyncMock(return_value=(Decimal("50.123456"),))
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await pitwall_cost_summary()

            assert len(result["entries"]) == 1
            entry = result["entries"][0]
            assert isinstance(entry["cost_usd"], float)
            assert entry["cost_usd"] == 50.123456


class TestPitwallRecentWorkloads:
    """Tests for pitwall_recent_workloads."""

    async def test_returns_workloads_key(self) -> None:
        """Verify response has 'workloads' key at top level."""
        with patch("pitwall.mcp.tools.cost.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await pitwall_recent_workloads()

            assert isinstance(result, dict)
            assert "workloads" in result
            assert isinstance(result["workloads"], list)

    async def test_passes_limit(self) -> None:
        """Verify limit parameter is passed to the query."""
        with patch("pitwall.mcp.tools.cost.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

            await pitwall_recent_workloads(limit=50)

            mock_conn.fetch.assert_called_once()
            call_args = mock_conn.fetch.call_args[0]
            assert 50 in call_args

    async def test_passes_state_filter(self) -> None:
        """Verify state filter is passed to the query."""
        with patch("pitwall.mcp.tools.cost.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

            await pitwall_recent_workloads(state="completed")

            mock_conn.fetch.assert_called_once()
            call_args = mock_conn.fetch.call_args[0]
            assert "w.state = $" in call_args[0]

    async def test_passes_capability_and_provider_filters(self) -> None:
        """Verify capability_id, provider_id, and provider_type filters are passed."""
        with patch("pitwall.mcp.tools.cost.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

            await pitwall_recent_workloads(
                capability_id="cap_emb",
                provider_id="prov_abc",
                provider_type="serverless_lb",
            )

            mock_conn.fetch.assert_called_once()
            call_args = mock_conn.fetch.call_args[0]
            query = call_args[0]
            assert "w.capability_id = $" in query
            assert "w.provider_id = $" in query
            assert "p.provider_type = $" in query

    async def test_workload_cost_fields_are_numeric(self) -> None:
        """Verify cost_estimate_usd and cost_actual_usd are JSON numbers, not strings."""
        with patch("pitwall.mcp.tools.cost.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            now = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
            mock_row = MagicMock()
            mock_row.__getitem__ = MagicMock(
                side_effect=lambda k: {
                    "id": "wkl_test",
                    "capability_id": "cap_emb",
                    "provider_id": "prov_abc",
                    "provider_type": "serverless_lb",
                    "type": "inference",
                    "state": "completed",
                    "runpod_job_id": "rp_job_123",
                    "idempotency_key": None,
                    "submitted_at": now,
                    "started_at": now,
                    "completed_at": now,
                    "execution_ms": 1500,
                    "queue_ms": 100,
                    "cold_start_ms": None,
                    "input_bytes": 500,
                    "output_bytes": 200,
                    "cost_estimate_usd": Decimal("0.005000"),
                    "cost_actual_usd": Decimal("0.004500"),
                    "error": None,
                    "langfuse_trace_id": "trace_abc",
                }[k]
            )

            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[mock_row])
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await pitwall_recent_workloads()

            assert len(result["workloads"]) == 1
            wl = result["workloads"][0]
            assert isinstance(wl["cost_estimate_usd"], float)
            assert isinstance(wl["cost_actual_usd"], float)
            assert wl["cost_estimate_usd"] == 0.005
            assert wl["cost_actual_usd"] == 0.0045


class TestCostReportingIntegration:
    """Integration-style tests verifying the service layer is called correctly."""

    async def test_cost_summary_calls_service_layer(self) -> None:
        """Verify pitwall_cost_summary calls the cost_summary_service function."""
        with patch("pitwall.mcp.tools.cost._cost_summary_service") as mock_service:
            mock_pool = MagicMock()
            mock_service.return_value = {"total_usd": 0.0, "entries": []}

            with patch("pitwall.mcp.tools.cost.get_pool", return_value=mock_pool):
                await pitwall_cost_summary(capability_class="llm")

            mock_service.assert_called_once()
            call_kwargs = mock_service.call_args[1]
            assert call_kwargs["capability_class"] == "llm"

    async def test_recent_workloads_calls_service_layer(self) -> None:
        """Verify pitwall_recent_workloads calls the recent_workloads_service function."""
        with patch("pitwall.mcp.tools.cost._recent_workloads_service") as mock_service:
            mock_pool = MagicMock()
            mock_service.return_value = {"workloads": []}

            with patch("pitwall.mcp.tools.cost.get_pool", return_value=mock_pool):
                await pitwall_recent_workloads(state="completed", limit=10)

            mock_service.assert_called_once()
            call_kwargs = mock_service.call_args[1]
            assert call_kwargs["state"] == "completed"
            assert call_kwargs["limit"] == 10
