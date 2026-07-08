"""Tests for ``pitwall-gpu-broker warm-volume`` CLI command.

Covers the spend-path polling loop:
- dry-run validation
- missing config (RUNPOD_API_KEY, RUNPOD_DATA_CENTER_ID)
- successful poll exit (pod exits cleanly)
- pod disappears during polling
- timeout waiting for pod exit

All tests are hermetic: no live RunPod API calls.
"""

from __future__ import annotations

import os
import subprocess
import sys
from argparse import Namespace
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pitwall.cost.budget_gate import BudgetAdmission

pytestmark = pytest.mark.anyio


class TestWarmVolumeCLI:
    """CLI-level tests for ``pitwall-gpu-broker warm-volume``."""

    def _cli(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run the pitwall CLI with the given argv and return the result."""
        full_env = dict(os.environ)
        if env is not None:
            full_env.update(env)
        full_env["PYTHONPATH"] = "src"
        return subprocess.run(
            [sys.executable, "-m", "pitwall", *argv],
            capture_output=True,
            text=True,
            env=full_env,
        )

    def test_missing_runpod_api_key_exits_with_error(self) -> None:
        """When RUNPOD_API_KEY is not set, CLI prints an error and returns 1."""
        result = self._cli(
            ["warm-volume", "--capability", "cap_test", "--volume-id", "vol_123"],
            env={
                "RUNPOD_API_KEY": "",
                "RUNPOD_DATA_CENTER_ID": "us-east-1",
                "PATH": os.environ.get("PATH", ""),
            },
        )
        assert result.returncode == 1
        assert "RUNPOD_API_KEY is required" in result.stderr

    def test_missing_runpod_data_center_id_exits_with_error(self) -> None:
        """When RUNPOD_DATA_CENTER_ID is not set, CLI prints an error and returns 1."""
        result = self._cli(
            ["warm-volume", "--capability", "cap_test", "--volume-id", "vol_123"],
            env={
                "RUNPOD_API_KEY": "test-key",
                "RUNPOD_DATA_CENTER_ID": "",
                "PATH": os.environ.get("PATH", ""),
            },
        )
        assert result.returncode == 1
        assert "RUNPOD_DATA_CENTER_ID is required" in result.stderr

    def test_dry_run_shows_params_and_exits_zero(self) -> None:
        """``--dry-run`` validates parameters and prints what would be done."""
        result = self._cli(
            [
                "warm-volume",
                "--capability",
                "cap_embedding_bge_m3",
                "--volume-id",
                "vol_abc123",
                "--provider",
                "prov_01",
                "--script",
                "default",
                "--dry-run",
            ],
            env={
                "RUNPOD_API_KEY": "test-key",
                "RUNPOD_DATA_CENTER_ID": "us-east-1",
                "PATH": os.environ.get("PATH", ""),
            },
        )
        assert result.returncode == 0
        assert "[dry-run] capability: cap_embedding_bge_m3" in result.stdout
        assert "[dry-run] volume_id: vol_abc123" in result.stdout
        assert "[dry-run] provider: prov_01" in result.stdout
        assert "[dry-run] script: default" in result.stdout

    def test_dry_run_missing_required_args_shows_error(self) -> None:
        """``--dry-run`` with missing required args shows usage error."""
        result = self._cli(
            ["warm-volume", "--dry-run"],
            env={
                "RUNPOD_API_KEY": "test-key",
                "RUNPOD_DATA_CENTER_ID": "us-east-1",
                "PATH": os.environ.get("PATH", ""),
            },
        )
        assert result.returncode != 0
        assert "error" in result.stderr.lower() or "required" in result.stderr.lower()


class TestWarmVolumeSpendPath:
    """Hermetic spend-path tests for ``warm-volume`` polling loop.

    These tests mock the RunPod pod lifecycle functions to cover:
    - successful poll exit (pod reaches EXITED state)
    - pod disappears during polling (get_pod_sync returns None)
    - timeout waiting for pod exit
    """

    def _make_mock_pool(
        self,
        provider_row: dict[str, Any] | None = None,
    ) -> MagicMock:
        """Create a properly mocked asyncpg pool.

        Sets up mock rows for both capability and provider lookups.
        """
        import datetime as dt

        capability_row = {
            "id": "cap_embedding_bge_m3",
            "name": "embedding.bge-m3",
            "version": "1.0.0",
            "class": "embedding",
            "cost_mode": "per_second",
            "config": {
                "description": "BGE-M3 embedding model",
                "input_schema": {"type": "object", "properties": {"texts": {"type": "array"}}},
                "output_schema": {"type": "object", "properties": {"dense": {"type": "array"}}},
                "defaults": {"execution_timeout_ms": 60000, "ttl_ms": 300000},
                "hints_supported": ["latency_sensitive", "cost_sensitive"],
            },
            "source": "api",
            "last_applied_yaml_hash": None,
            "enabled": True,
            "created_at": dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC),
            "updated_at": dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC),
        }

        if provider_row is None:
            provider_row = {
                "id": "prov_test",
                "capability_id": "cap_embedding_bge_m3",
                "name": "test-provider",
                "provider_type": "serverless_queue",
                "runpod_endpoint_id": None,
                "runpod_template_id": None,
                "region": "us-east-1",
                "cloud_type": "ALL",
                "config": {
                    "gpu_class": "NVIDIA L4",
                    "cost": {"per_second_active": "0.001"},
                },
                "priority": 1,
                "enabled": True,
                "health_status": "healthy",
                "consecutive_failures": 0,
                "cooldown_trips": 0,
                "cold_start_p50_ms": None,
                "cold_start_p95_ms": None,
                "recent_error_rate": 0,
                "cooldown_until": None,
                "source": "api",
                "last_applied_yaml_hash": None,
                "updated_at": dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC),
            }

        def fetchrow_side_effect(sql: str, *args: object) -> Any:
            if "sum(" in sql.lower() and "pitwall.workloads" in sql.lower():
                return {"s": Decimal("0")}
            if "capabilities" in sql:
                return capability_row
            return capability_row

        def fetch_side_effect(sql: str, *args: object) -> list[dict[str, Any]]:
            if "providers" in sql:
                return [provider_row]
            return [provider_row]

        conn = MagicMock()
        conn.execute = AsyncMock(return_value="SELECT 1")
        conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
        conn.fetch = AsyncMock(side_effect=fetch_side_effect)
        conn.fetchval = AsyncMock(return_value="wkl_warm_volume_test")

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

    def _make_ready_pod(
        self,
        pod_id: str,
        desired_status: str = "RUNNING",
        runtime_status: str | None = "RUNNING",
        has_ports: bool = True,
    ) -> dict[str, Any]:
        """Create a pod dict with readiness signals for wait_for_pod_runtime_sync."""
        from datetime import UTC, datetime

        pod: dict[str, Any] = {
            "id": pod_id,
            "desiredStatus": desired_status,
        }

        if runtime_status is not None:
            pod["runtime"] = {
                "status": runtime_status,
                "uptimeInSeconds": 30,
            }
            pod["readiness"] = {
                "runtime_seen_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "port_mappings_seen_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "probe_passed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "probe_method": "runpod_proxy",
            }
            if has_ports:
                pod["runtime"]["ports"] = [{"ip": "127.0.0.1", "privatePort": 8000}]
                pod["portMappings"] = [
                    {"ip": "127.0.0.1", "privatePort": 8000, "publicPort": 12345}
                ]
        else:
            pod["runtime"] = None
        return pod

    async def test_budget_admission_precedes_pod_create(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The paid RunPod create path is reached only after budget admission."""
        from pitwall.cli import _warm_volume_async

        monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
        monkeypatch.setenv("RUNPOD_DATA_CENTER_ID", "us-east-1")
        monkeypatch.setenv(
            "PITWALL_CLOUD_WORKER_IMAGE", "ghcr.io/example/pitwall/cloud-worker:test"
        )

        mock_pool = self._make_mock_pool()
        calls: list[str] = []
        admission_kwargs: dict[str, Any] = {}

        class FakeBudgetGate:
            def __init__(self, pool: object, **kwargs: Any) -> None:
                assert pool is mock_pool
                assert kwargs["monthly_budget_usd"] > 0
                assert kwargs["per_request_max_usd"] > 0

            async def try_launch_admission(self, **kwargs: Any) -> BudgetAdmission:
                assert calls == []
                calls.append("budget_gate")
                admission_kwargs.update(kwargs)
                return BudgetAdmission(workload_id="wkl_warm_order", is_new=True)

        def mock_create_pod_sync(**kwargs: Any) -> dict[str, Any]:
            assert calls == ["budget_gate"]
            calls.append("create_pod_with_fallback_sync")
            assert kwargs["network_volume_id"] == "vol_abc123"
            return self._make_ready_pod("pod_budget_order")

        def mock_terminate_pod_sync(pod_id_arg: str) -> None:
            assert pod_id_arg == "pod_budget_order"
            calls.append("terminate_pod_sync")

        with (
            patch("pitwall.db.get_pool", AsyncMock(return_value=mock_pool)),
            patch("pitwall.cost.budget_gate.BudgetGate", FakeBudgetGate),
            patch("pitwall.cli.create_pod_with_fallback_sync", mock_create_pod_sync),
            patch("pitwall.cli.get_pod_sync", return_value=None),
            patch("pitwall.cli.terminate_pod_sync", mock_terminate_pod_sync),
        ):
            args = Namespace(
                capability="cap_embedding_bge_m3",
                volume_id="vol_abc123",
                provider=None,
                script="default",
                dry_run=False,
                timeout=300,
            )
            return_code = await _warm_volume_async(args)

        assert return_code == 0
        assert calls == [
            "budget_gate",
            "create_pod_with_fallback_sync",
            "terminate_pod_sync",
        ]
        assert admission_kwargs == {
            "capability_id": "cap_embedding_bge_m3",
            "provider_id": "prov_test",
            "estimate_usd": Decimal("0.300000"),
            "workload_type": "warm_volume",
        }

    async def test_pod_create_failure_closes_admitted_workload_with_zero_actual(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed RunPod create does not leave the budget reservation active."""
        from pitwall.cli import _warm_volume_async

        monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
        monkeypatch.setenv("RUNPOD_DATA_CENTER_ID", "us-east-1")
        monkeypatch.setenv(
            "PITWALL_CLOUD_WORKER_IMAGE", "ghcr.io/example/pitwall/cloud-worker:test"
        )

        mock_pool = self._make_mock_pool()
        conn = mock_pool.acquire.return_value.__aenter__.return_value
        calls: list[str] = []

        class FakeBudgetGate:
            def __init__(self, pool: object, **_kwargs: Any) -> None:
                assert pool is mock_pool

            async def try_launch_admission(self, **_kwargs: Any) -> BudgetAdmission:
                calls.append("budget_gate")
                return BudgetAdmission(workload_id="wkl_warm_create_failed", is_new=True)

        def fail_create_pod_sync(**_kwargs: Any) -> dict[str, Any]:
            assert calls == ["budget_gate"]
            calls.append("create_pod_with_fallback_sync")
            raise RuntimeError("create failed")

        with (
            patch("pitwall.db.get_pool", AsyncMock(return_value=mock_pool)),
            patch("pitwall.cost.budget_gate.BudgetGate", FakeBudgetGate),
            patch("pitwall.cli.create_pod_with_fallback_sync", fail_create_pod_sync),
        ):
            args = Namespace(
                capability="cap_embedding_bge_m3",
                volume_id="vol_abc123",
                provider=None,
                script="default",
                dry_run=False,
                timeout=300,
            )
            return_code = await _warm_volume_async(args)

        assert return_code == 2
        assert calls == ["budget_gate", "create_pod_with_fallback_sync"]
        cleanup_args = conn.execute.await_args.args
        sql, workload_id, completed_at, cost_actual_usd, error_payload = cleanup_args
        assert "UPDATE pitwall.workloads" in sql
        assert "cost_actual_usd = $3" in sql
        assert workload_id == "wkl_warm_create_failed"
        assert completed_at.tzinfo is not None
        assert cost_actual_usd == Decimal("0")
        assert error_payload == {
            "phase": "warm_volume_pod_create",
            "error_type": "RuntimeError",
            "message": "create failed",
        }

    async def test_poll_loop_exits_cleanly_on_pod_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When pod reaches EXITED state, poll loop exits cleanly."""
        from pitwall.cli import _warm_volume_async

        monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
        monkeypatch.setenv("RUNPOD_DATA_CENTER_ID", "us-east-1")
        monkeypatch.setenv(
            "PITWALL_CLOUD_WORKER_IMAGE", "ghcr.io/example/pitwall/cloud-worker:test"
        )

        mock_pool = self._make_mock_pool()

        pod_id = "pod_exit_clean_123"

        def mock_create_pod_sync(**kwargs: Any) -> dict[str, Any]:
            return self._make_ready_pod(pod_id)

        poll_responses = [
            self._make_ready_pod(pod_id, desired_status="RUNNING", runtime_status="RUNNING"),
            self._make_ready_pod(pod_id, desired_status="EXITED", runtime_status="EXITED"),
        ]

        def mock_get_pod_sync(pod_id_arg: str) -> dict[str, Any] | None:
            if poll_responses:
                return poll_responses.pop(0)
            return None

        terminate_called: list[str] = []

        def mock_terminate_pod_sync(pod_id_arg: str) -> None:
            terminate_called.append(pod_id_arg)

        with (
            patch("pitwall.db.get_pool", AsyncMock(return_value=mock_pool)),
            patch("pitwall.cli.create_pod_with_fallback_sync", mock_create_pod_sync),
            patch("pitwall.cli.get_pod_sync", mock_get_pod_sync),
            patch("pitwall.cli.terminate_pod_sync", mock_terminate_pod_sync),
        ):
            args = Namespace(
                capability="cap_embedding_bge_m3",
                volume_id="vol_abc123",
                provider=None,
                script="default",
                dry_run=False,
                timeout=300,
            )
            return_code = await _warm_volume_async(args)

        assert return_code == 0
        assert terminate_called == [pod_id]

    async def test_poll_loop_handles_pod_disappearing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When get_pod_sync returns None, poll loop treats it as clean exit."""
        from pitwall.cli import _warm_volume_async

        monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
        monkeypatch.setenv("RUNPOD_DATA_CENTER_ID", "us-east-1")
        monkeypatch.setenv(
            "PITWALL_CLOUD_WORKER_IMAGE", "ghcr.io/example/pitwall/cloud-worker:test"
        )

        mock_pool = self._make_mock_pool()

        pod_id = "pod_disappear_456"

        def mock_create_pod_sync(**kwargs: Any) -> dict[str, Any]:
            return self._make_ready_pod(pod_id)

        def mock_get_pod_sync(pod_id_arg: str) -> dict[str, Any] | None:
            return None

        terminate_called: list[str] = []

        def mock_terminate_pod_sync(pod_id_arg: str) -> None:
            terminate_called.append(pod_id_arg)

        with (
            patch("pitwall.db.get_pool", AsyncMock(return_value=mock_pool)),
            patch("pitwall.cli.create_pod_with_fallback_sync", mock_create_pod_sync),
            patch("pitwall.cli.get_pod_sync", mock_get_pod_sync),
            patch("pitwall.cli.terminate_pod_sync", mock_terminate_pod_sync),
        ):
            args = Namespace(
                capability="cap_embedding_bge_m3",
                volume_id="vol_abc123",
                provider=None,
                script="default",
                dry_run=False,
                timeout=300,
            )
            return_code = await _warm_volume_async(args)

        assert return_code == 0
        assert terminate_called == [pod_id]

    async def test_poll_loop_respects_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When deadline passes before pod exits, poll loop times out."""
        from pitwall.cli import _warm_volume_async

        monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
        monkeypatch.setenv("RUNPOD_DATA_CENTER_ID", "us-east-1")
        monkeypatch.setenv(
            "PITWALL_CLOUD_WORKER_IMAGE", "ghcr.io/example/pitwall/cloud-worker:test"
        )

        mock_pool = self._make_mock_pool()

        pod_id = "pod_timeout_789"

        def mock_create_pod_sync(**kwargs: Any) -> dict[str, Any]:
            return self._make_ready_pod(pod_id)

        def mock_get_pod_sync(pod_id_arg: str) -> dict[str, Any] | None:
            return self._make_ready_pod(pod_id, desired_status="RUNNING", runtime_status="RUNNING")

        terminate_called: list[str] = []

        def mock_terminate_pod_sync(pod_id_arg: str) -> None:
            terminate_called.append(pod_id_arg)

        with (
            patch("pitwall.db.get_pool", AsyncMock(return_value=mock_pool)),
            patch("pitwall.cli.create_pod_with_fallback_sync", mock_create_pod_sync),
            patch("pitwall.cli.get_pod_sync", mock_get_pod_sync),
            patch("pitwall.cli.terminate_pod_sync", mock_terminate_pod_sync),
        ):
            args = Namespace(
                capability="cap_embedding_bge_m3",
                volume_id="vol_abc123",
                provider=None,
                script="default",
                dry_run=False,
                timeout=1,
            )
            return_code = await _warm_volume_async(args)

        assert return_code == 0
        assert terminate_called == [pod_id]

    async def test_poll_loop_terminates_on_desired_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When pod desiredStatus=EXITED (without runtime EXITED), poll loop exits."""
        from pitwall.cli import _warm_volume_async

        monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
        monkeypatch.setenv("RUNPOD_DATA_CENTER_ID", "us-east-1")
        monkeypatch.setenv(
            "PITWALL_CLOUD_WORKER_IMAGE", "ghcr.io/example/pitwall/cloud-worker:test"
        )

        mock_pool = self._make_mock_pool()

        pod_id = "pod_desired_exit_999"

        def mock_create_pod_sync(**kwargs: Any) -> dict[str, Any]:
            return self._make_ready_pod(pod_id)

        poll_responses = [
            self._make_ready_pod(pod_id, desired_status="EXITED", runtime_status=None),
        ]

        def mock_get_pod_sync(pod_id_arg: str) -> dict[str, Any] | None:
            if poll_responses:
                return poll_responses.pop(0)
            return None

        terminate_called: list[str] = []

        def mock_terminate_pod_sync(pod_id_arg: str) -> None:
            terminate_called.append(pod_id_arg)

        with (
            patch("pitwall.db.get_pool", AsyncMock(return_value=mock_pool)),
            patch("pitwall.cli.create_pod_with_fallback_sync", mock_create_pod_sync),
            patch("pitwall.cli.get_pod_sync", mock_get_pod_sync),
            patch("pitwall.cli.terminate_pod_sync", mock_terminate_pod_sync),
        ):
            args = Namespace(
                capability="cap_embedding_bge_m3",
                volume_id="vol_abc123",
                provider=None,
                script="default",
                dry_run=False,
                timeout=300,
            )
            return_code = await _warm_volume_async(args)

        assert return_code == 0
        assert terminate_called == [pod_id]
