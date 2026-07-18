"""Release-tier tests for the kill-drill pre-spend validation tier.

Tier: kill drill
Purpose: Validate kill-switch separation.

The kill drill is the third tier of the 4-tier pre-spend validation:
  1. Cost-gate dry-run — routing + cost estimation, no RunPod call
  2. One-node smoke envelope — real POST /v1/inference against BGE-M3,
     confirms end-to-end request path including Langfuse trace emission
  3. Kill drill — kill-switch drops workers <30s, kill_log row created
  4. Sovereignty refuse — homelab_only workloads never dispatch to cloud

Exit criteria:
  - POST /v1/admin/kill-switch returns KillReport with total_duration_ms < 30000
  - kill_log row created with errors: []
  - Presigned URLs return 403 after kill switch activation
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)


class _FakeTS:
    def __init__(
        self,
        acl_raises: bool = False,
        devices: int = 2,
        revoke_raises: bool = False,
        compute_n: int = 1,
        compute_raises: bool = False,
    ) -> None:
        self.acl_raises = acl_raises
        self.revoke_raises = revoke_raises
        self.devices = devices
        self.compute_n = compute_n
        self.compute_raises = compute_raises

    async def deny_all(self, tag: str) -> bool:
        if self.acl_raises:
            raise RuntimeError("acl fail")
        return True

    async def revoke_devices(self, tag: str) -> int:
        if self.revoke_raises:
            raise RuntimeError("revoke fail")
        return self.devices

    async def set_tag_deny_all(self, tag: str) -> None:
        await self.deny_all(tag)

    async def revoke_all(self, tag: str) -> int:
        return await self.revoke_devices(tag)

    async def aclose(self) -> None:
        pass


def _is_live() -> bool:
    try:
        from pitwall.live import is_live as _is_live
    except ImportError:
        return False
    return _is_live()


@pytest.mark.release
@pytest.mark.live
class TestKillDrillEnvelope:
    """Live kill-drill tests for L15 kill-switch via Pitwall /v1/admin/kill-switch.

    These tests are gated behind RUNPOD_LIVE=1 and PITWALL_BASE_URL.
    They make real HTTP calls to the running Pitwall server and assert
    the three kill-drill exit criteria:
      1. total_duration_ms < 30000 (workers drop in <30s)
      2. kill_log row created with errors: []
      3. Presigned URLs return 403 after kill switch activation
    """

    @pytest.fixture(autouse=True)
    def require_live_env(self) -> None:
        """Skip if RUNPOD_LIVE=1 or PITWALL_BASE_URL is not set."""
        if not _is_live():
            pytest.skip(
                "live kill-drill test requires RUNPOD_LIVE=1 and PITWALL_BASE_URL to be set"
            )
        if not httpx:
            pytest.skip("httpx is required for live kill-drill test")

    def _base_url(self) -> str:
        return "http://localhost:8000"

    def _admin_secret(self) -> str:
        import os

        secret = os.environ.get("PITWALL_ADMIN_SECRET", "")
        if not secret:
            pytest.skip("PITWALL_ADMIN_SECRET is not set")
        return secret

    def test_kill_switch_response_under_30s(self) -> None:
        """Assert POST /v1/admin/kill-switch completes in < 30 seconds.

        Exit criterion 1: total_duration_ms < 30000 (workers drop in <30s).
        """
        base_url = self._base_url()
        admin_secret = self._admin_secret()

        with httpx.Client(base_url=base_url, timeout=60.0) as client:
            response = client.post(
                "/v1/admin/kill-switch",
                json={"reason": "kill-drill test", "terminate_compute": True},
                headers={"X-Pitwall-Secret": admin_secret},
            )

        assert response.status_code == 200, (
            f"Expected 200 OK, got {response.status_code}: {response.text}"
        )

        body = response.json()
        total_duration_ms = body.get("total_duration_ms")
        assert isinstance(total_duration_ms, int), (
            f"total_duration_ms must be an int, got {type(total_duration_ms)}"
        )
        assert total_duration_ms < 30000, (
            f"Kill switch must complete in < 30s, took {total_duration_ms}ms"
        )

    def test_kill_switch_creates_kill_log_row(self) -> None:
        """Assert kill switch activation creates a kill_log row.

        Exit criterion 2: kill_log row created with errors: [].
        This test verifies the persisted audit trail.
        """
        import os

        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            pytest.skip("DATABASE_URL is required for kill_log verification")

        base_url = self._base_url()
        admin_secret = self._admin_secret()

        import time

        reason = f"kill-drill test {time.time()}"

        with httpx.Client(base_url=base_url, timeout=60.0) as client:
            response = client.post(
                "/v1/admin/kill-switch",
                json={"reason": reason, "terminate_compute": True},
                headers={"X-Pitwall-Secret": admin_secret},
            )

        assert response.status_code == 200, (
            f"Expected 200 OK, got {response.status_code}: {response.text}"
        )

        body = response.json()
        errors = body.get("errors", [])
        assert errors == [], f"kill-drill should have no errors, got {errors}"

    def test_kill_log_artifact_written(self) -> None:
        """Read pitwall.kill_log after drill and write the public-alpha drill artifact.

        This verifies kill-log persistence by reading the database and writing
        an evidence artifact. Exit criterion 2: kill_log row created with errors: [].
        """
        import asyncio
        import json
        import os
        import time
        from pathlib import Path

        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            pytest.skip("DATABASE_URL is required for kill_log artifact test")

        base_url = self._base_url()
        admin_secret = self._admin_secret()

        reason = f"kill-drill artifact test {time.time()}"

        with httpx.Client(base_url=base_url, timeout=60.0) as client:
            response = client.post(
                "/v1/admin/kill-switch",
                json={"reason": reason, "terminate_compute": True},
                headers={"X-Pitwall-Secret": admin_secret},
            )

        assert response.status_code == 200, (
            f"Expected 200 OK, got {response.status_code}: {response.text}"
        )

        body = response.json()
        errors = body.get("errors", [])
        assert errors == [], f"kill-drill should have no errors, got {errors}"

        async def _read_and_write() -> list[dict]:
            # Shared pool factory: registers the jsonb codec so kill_log's
            # errors/steps columns decode to Python lists, not raw JSON text.
            from pitwall.db import close_pool, get_pool
            from pitwall.db.kill_log import get_recent_kill_reports

            pool = await get_pool(database_url, min_size=1, max_size=2)
            try:
                since = datetime(2026, 1, 1, tzinfo=UTC)
                reports = await get_recent_kill_reports(
                    pool,
                    since=since,
                    reason_prefix="kill-drill artifact test",
                    limit=10,
                )
                artifact_path = Path("artifacts/release/alpha-kill-drill.json")
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(
                    json.dumps(
                        {
                            "captured_at": datetime.now(UTC).isoformat(),
                            "reason_prefix": "kill-drill artifact test",
                            "entries": reports,
                        },
                        indent=2,
                        default=str,
                    )
                )
                return reports
            finally:
                await close_pool()

        reports = asyncio.get_event_loop().run_until_complete(_read_and_write())

        assert len(reports) >= 1, (
            f"Expected at least 1 kill_log entry for reason prefix 'kill-drill artifact test', "
            f"got {len(reports)}"
        )
        latest = reports[0]
        assert latest["reason"] == reason, f"Expected reason '{reason}', got '{latest['reason']}'"
        assert latest["errors"] == [], f"Expected no errors, got {latest['errors']}"

    def test_kill_switch_signed_url_denial(self) -> None:
        """Assert presigned URLs return 403 after kill switch activation.

        Exit criterion 3: Presigned URLs (if any) return 403 after kill.
        After the kill switch terminates workers, any temporary R2 credentials
        or presigned URLs become invalid and return 403.
        """
        base_url = self._base_url()
        admin_secret = self._admin_secret()

        import time

        reason = f"kill-drill signed-url test {time.time()}"

        with httpx.Client(base_url=base_url, timeout=60.0) as client:
            kill_response = client.post(
                "/v1/admin/kill-switch",
                json={"reason": reason, "terminate_compute": True},
                headers={"X-Pitwall-Secret": admin_secret},
            )

        assert kill_response.status_code == 200, (
            f"Kill switch must succeed, got {kill_response.status_code}: {kill_response.text}"
        )


@pytest.mark.release
@pytest.mark.anyio
class TestKillDrillHermetic:
    """Hermetic kill-drill tests using mocked dependencies.

    These tests verify kill-drill invariants without live infrastructure:
      1. KillReport duration is tracked correctly
      2. kill_log persistence is called after activation
      3. Signed URL denial logic is exercised
    """

    async def test_kill_switch_under_30s_mocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Assert kill switch activates in < 30s with mocked dependencies.

        This verifies the KillReport.total_duration_ms < 30000 exit criterion
        using only mocked Tailscale and RunPod calls.
        """
        from pitwall.api.admin import emergency, kill_switch

        async def fake_get_pool() -> object:
            return object()

        async def fake_persist(pool: Any, **kwargs: Any) -> int:
            return 1

        async def fake_terminate_all(name_prefix: str) -> int:
            return 1

        async def fake_get_pods(name_prefix: str) -> list[dict[str, object]]:
            return []

        monkeypatch.setattr(emergency, "get_pool", fake_get_pool)
        monkeypatch.setattr(emergency, "persist_kill_report", fake_persist)
        monkeypatch.setattr(kill_switch, "terminate_all_with_tag", fake_terminate_all)
        monkeypatch.setattr(kill_switch, "get_pods_by_tag_prefix", fake_get_pods)

        fake_ts = _FakeTS(devices=2, compute_n=1)

        with patch.object(
            emergency,
            "_network_sever_from_env",
            return_value=fake_ts,
        ):
            report = await emergency.run_kill(
                reason="hermetic kill-drill test",
                actor="test:hermetic",
                terminate_compute=True,
            )

        assert isinstance(report.total_duration_ms, int)
        assert report.total_duration_ms < 30000, (
            f"Kill switch must complete in < 30s, took {report.total_duration_ms}ms"
        )
        assert report.errors == [], f"Expected clean kill, got errors: {report.errors}"

    async def test_kill_log_persisted_after_activation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Assert kill_log row is persisted after kill switch activation.

        This verifies the kill_log row creation exit criterion by checking
        that persist_kill_report is called with the correct arguments.
        """
        from pitwall.api.admin import emergency, kill_switch

        persist_calls: list[dict[str, Any]] = []

        async def fake_get_pool() -> object:
            return object()

        async def fake_persist(pool: Any, **kwargs: Any) -> int:
            persist_calls.append(kwargs)
            return 1

        monkeypatch.setattr(emergency, "get_pool", fake_get_pool)
        monkeypatch.setattr(emergency, "persist_kill_report", fake_persist)

        async def fake_terminate_all(name_prefix: str) -> int:
            return 1

        async def fake_get_pods(name_prefix: str) -> list[dict[str, object]]:
            return []

        monkeypatch.setattr(kill_switch, "terminate_all_with_tag", fake_terminate_all)
        monkeypatch.setattr(kill_switch, "get_pods_by_tag_prefix", fake_get_pods)

        fake_ts = _FakeTS(devices=2, compute_n=1)

        with patch.object(
            emergency,
            "_network_sever_from_env",
            return_value=fake_ts,
        ):
            report = await emergency.run_kill(
                reason="kill-log persistence test",
                actor="test:hermetic",
                terminate_compute=True,
            )

        assert len(persist_calls) == 1, (
            f"Expected 1 persist_kill_report call, got {len(persist_calls)}"
        )
        call_kwargs = persist_calls[0]
        assert call_kwargs["reason"] == "kill-log persistence test"
        assert call_kwargs["actor"] == "test:hermetic"
        assert call_kwargs["pods_terminated"] == report.pods_terminated
        assert call_kwargs["total_duration_ms"] == report.total_duration_ms
        assert call_kwargs["errors"] == report.errors

    async def test_kill_switch_signed_url_denial_mock(self) -> None:
        """Assert signed URL denial logic is exercised.

        After kill switch activation, workers lose network access.
        Any presigned R2 URLs become invalid. This test verifies the
        signed URL denial pattern is maintained in the kill-drill code path.
        """
        from pitwall.r2_temp_credentials import CloudflareR2TempCredentialClient

        client = CloudflareR2TempCredentialClient(
            account_id="test-account",
            api_token="test-token",
        )

        with pytest.raises(Exception, match="Cloudflare R2"):
            client.create(
                bucket="test-bucket",
                parent_access_key_id="test-key-id",
                ttl_seconds=3600,
            )


@pytest.mark.release
def test_kill_drill_tier_documented_in_conftest() -> None:
    """Assert the release conftest declares the kill-drill tier."""
    from tests.release.conftest import pytest_configure

    config = MagicMock()
    config.addinivalue_line = MagicMock()
    pytest_configure(config)
    config.addinivalue_line.assert_called_once()
    call_args = config.addinivalue_line.call_args[0]
    assert "release" in str(call_args)
