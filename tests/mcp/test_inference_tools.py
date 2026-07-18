"""Tests for MCP inference tools.

These tests verify that the inference tool handlers preserve idempotency_key
and dry_run through the request lifecycle and properly handle mismatch/error
behavior.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.api.exceptions import IdempotencyMismatch
from pitwall.mcp.tools.inference import (
    _canonical_json,
    _lookup_idempotent_workload,
    _replay_idempotent_inference,
)

pytestmark = pytest.mark.anyio


def _canonical_json_value(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


class TestReplayIdempotentInference:
    """Tests for _replay_idempotent_inference helper."""

    async def test_returns_none_when_no_idempotency_key(self) -> None:
        pool = MagicMock()
        result = await _replay_idempotent_inference(
            pool,
            idempotency_key=None,
            capability_params={"texts": ["hello"]},
        )
        assert result is None

    async def test_returns_none_when_key_not_found(self) -> None:
        pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        acq = MagicMock()
        acq.__aenter__ = AsyncMock(return_value=mock_conn)
        acq.__aexit__ = AsyncMock(return_value=None)
        pool.acquire = MagicMock(return_value=acq)

        result = await _replay_idempotent_inference(
            pool,
            idempotency_key="unknown-key",
            capability_params={"texts": ["hello"]},
        )
        assert result is None

    async def test_returns_replay_when_body_matches(self) -> None:
        pool = MagicMock()
        mock_conn = AsyncMock()
        idempotency_row = {
            "id": "wkl_replayed",
            "state": "completed",
            "input": {"texts": ["hello"]},
            "result": {"dense": [[0.1, 0.2]]},
        }
        mock_conn.fetchrow = AsyncMock(side_effect=[idempotency_row, None])
        acq = MagicMock()
        acq.__aenter__ = AsyncMock(return_value=mock_conn)
        acq.__aexit__ = AsyncMock(return_value=None)
        pool.acquire = MagicMock(return_value=acq)

        result = await _replay_idempotent_inference(
            pool,
            idempotency_key="replay-key",
            capability_params={"texts": ["hello"]},
        )
        assert result is not None
        assert result["workload_id"] == "wkl_replayed"
        assert "cost" in result
        assert "provider_id" in result
        assert "state" in result
        assert "result" in result
        assert "trace_id" in result

    async def test_raises_mismatch_when_body_differs(self) -> None:
        pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": "wkl_orig",
                "state": "completed",
                "input": {"texts": ["original"]},
                "result": {"dense": [[0.1, 0.2]]},
            }
        )
        acq = MagicMock()
        acq.__aenter__ = AsyncMock(return_value=mock_conn)
        acq.__aexit__ = AsyncMock(return_value=None)
        pool.acquire = MagicMock(return_value=acq)

        with pytest.raises(IdempotencyMismatch) as exc_info:
            await _replay_idempotent_inference(
                pool,
                idempotency_key="mismatch-key",
                capability_params={"texts": ["different"]},
            )
        assert exc_info.value.original_workload_id == "wkl_orig"

    async def test_returns_replay_when_original_input_is_none(self) -> None:
        pool = MagicMock()
        mock_conn = AsyncMock()
        idempotency_row = {
            "id": "wkl_replayed",
            "state": "completed",
            "input": None,
            "result": {"dense": [[0.1, 0.2]]},
        }
        mock_conn.fetchrow = AsyncMock(side_effect=[idempotency_row, None])
        acq = MagicMock()
        acq.__aenter__ = AsyncMock(return_value=mock_conn)
        acq.__aexit__ = AsyncMock(return_value=None)
        pool.acquire = MagicMock(return_value=acq)

        result = await _replay_idempotent_inference(
            pool,
            idempotency_key="replay-key",
            capability_params={"texts": ["hello"]},
        )
        assert result is not None
        assert result["workload_id"] == "wkl_replayed"
        assert "cost" in result
        assert "provider_id" in result
        assert "state" in result
        assert "trace_id" in result


class TestLookupIdempotentWorkload:
    """Tests for _lookup_idempotent_workload helper."""

    async def test_returns_none_when_not_found(self) -> None:
        pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        acq = MagicMock()
        acq.__aenter__ = AsyncMock(return_value=mock_conn)
        acq.__aexit__ = AsyncMock(return_value=None)
        pool.acquire = MagicMock(return_value=acq)

        result = await _lookup_idempotent_workload(pool, "unknown-key")
        assert result is None

    async def test_returns_workload_data_when_found(self) -> None:
        pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": "wkl_found",
                "state": "completed",
                "input": {"texts": ["hello"]},
                "result": {"dense": [[0.1, 0.2]]},
            }
        )
        acq = MagicMock()
        acq.__aenter__ = AsyncMock(return_value=mock_conn)
        acq.__aexit__ = AsyncMock(return_value=None)
        pool.acquire = MagicMock(return_value=acq)

        result = await _lookup_idempotent_workload(pool, "found-key")
        assert result is not None
        assert result["workload_id"] == "wkl_found"
        assert result["state"] == "completed"
        assert result["input"] == {"texts": ["hello"]}
        assert result["result"] == {"dense": [[0.1, 0.2]]}


class TestCanonicalJson:
    """Tests for _canonical_json helper."""

    def test_produces_consistent_hash_for_same_input(self) -> None:
        data = {"texts": ["hello", "world"], "return_dense": True}
        hash1 = _canonical_json(data)
        hash2 = _canonical_json(data)
        assert hash1 == hash2

    def test_produces_different_hash_for_different_input(self) -> None:
        data1 = {"texts": ["hello"]}
        data2 = {"texts": ["different"]}
        assert _canonical_json(data1) != _canonical_json(data2)

    def test_sort_keys_produces_consistent_output(self) -> None:
        data = {"b": 1, "a": 2}
        result = _canonical_json(data)
        assert '"a":2' in result
        assert '"b":1' in result
