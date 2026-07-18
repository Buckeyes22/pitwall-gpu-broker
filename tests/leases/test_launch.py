from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any

import pytest

from pitwall.api.leases import launch
from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.cost.budget_gate import BudgetRejected, BudgetSnapshot
from pitwall.runpod_client import pods, templates


def _capability() -> Capability:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    return Capability(
        id="cap_llm_qwen3",
        name="llm.qwen3-32b",
        version="1",
        class_=CapabilityClass.LLM,
        cost_mode=CostMode.PER_SECOND,
        source=CapabilitySource.API,
        created_at=now,
        updated_at=now,
    )


def _provider(config: dict[str, Any] | None = None) -> Provider:
    return Provider(
        id="prov_qwen3_h100",
        capability_id="cap_llm_qwen3",
        name="qwen3-h100-pod-us-ca",
        provider_type=ProviderType.POD_LEASE,
        region="US-CA-2",
        cloud_type="SECURE",
        config=config
        or {
            "image_ref": "ghcr.io/acme/pitwall-worker:qwen3",
            "template_name": "pitwall-qwen3-h100",
            "gpu_type_priority": ["NVIDIA H100 80GB HBM3", "NVIDIA L4"],
            "container_disk_gb": 80,
            "volume_id": "vol-model-cache",
            "volume_mount": "/workspace",
            "ports": {"http": [8000], "tcp": [22]},
            "env_vars": {"VLLM_MODEL": "Qwen/Qwen3-32B"},
            "cost": {"per_second_active": "0.002"},
        },
        priority=1,
        source=CapabilitySource.API,
        updated_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
    )


def test_template_env_keys_use_pitwall_identity_names() -> None:
    assert "PITWALL_CAPABILITY" in templates._TEMPLATE_ENV_KEYS
    assert "PITWALL_CAPABILITY_ID" in templates._TEMPLATE_ENV_KEYS
    assert "PITWALL_PROVIDER" in templates._TEMPLATE_ENV_KEYS
    assert "PITWALL_PROVIDER_ID" in templates._TEMPLATE_ENV_KEYS
    assert "AWS_SESSION_TOKEN" in templates._TEMPLATE_ENV_KEYS
    assert "R2_CREDENTIAL_EXPIRES_AT" in templates._TEMPLATE_ENV_KEYS
    assert "CLOUD_CAPABILITY" not in templates._TEMPLATE_ENV_KEYS
    assert "CLOUD_PROVIDER" not in templates._TEMPLATE_ENV_KEYS
    assert "R2_ACCESS_KEY" not in templates._TEMPLATE_ENV_KEYS
    assert "R2_SECRET_KEY" not in templates._TEMPLATE_ENV_KEYS


def test_env_for_pod_uses_capability_and_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://pitwall-redis/4")
    monkeypatch.setenv("R2_ENDPOINT", "https://r2.example.test")
    monkeypatch.setenv("R2_ACCESS_KEY", "parent-access-key")
    monkeypatch.setenv("R2_SECRET_KEY", "parent-secret-key")

    env = launch._env_for_pod(
        _capability(),
        _provider(),
        request_id="req_123",
        extra_env={"OPENAI_SERVED_MODEL_NAME_OVERRIDE": "qwen3"},
    )

    assert env["PITWALL_CAPABILITY"] == "llm"
    assert env["PITWALL_CAPABILITY_ID"] == "cap_llm_qwen3"
    assert env["PITWALL_CAPABILITY_NAME"] == "llm.qwen3-32b"
    assert env["PITWALL_PROVIDER"] == "qwen3-h100-pod-us-ca"
    assert env["PITWALL_PROVIDER_ID"] == "prov_qwen3_h100"
    assert env["PITWALL_PROVIDER_TYPE"] == "pod_lease"
    assert env["PITWALL_REQUEST_ID"] == "req_123"
    assert env["REDIS_URL"] == "redis://pitwall-redis/4"
    assert env["R2_ENDPOINT"] == "https://r2.example.test"
    assert "R2_ACCESS_KEY" not in env
    assert "R2_SECRET_KEY" not in env
    assert env["VLLM_MODEL"] == "Qwen/Qwen3-32B"
    assert env["OPENAI_SERVED_MODEL_NAME_OVERRIDE"] == "qwen3"
    assert "CLOUD_CAPABILITY" not in env
    assert "CLOUD_PROVIDER" not in env


def test_env_for_pod_rejects_identity_overrides() -> None:
    provider = _provider({"env_vars": {"PITWALL_PROVIDER_ID": "forged"}})

    with pytest.raises(launch.InvalidProviderConfig, match="identity key"):
        launch._env_for_pod(_capability(), provider)


def test_env_for_pod_rejects_storage_credential_overrides() -> None:
    provider = _provider({"env_vars": {"AWS_SECRET_ACCESS_KEY": "forged"}})

    with pytest.raises(launch.InvalidProviderConfig, match="storage credential key"):
        launch._env_for_pod(_capability(), provider)


def test_env_for_pod_adds_vended_staging_store_credentials() -> None:
    class FakeStagingStore:
        def vend_pod_credentials(self) -> dict[str, str]:
            return {
                "AWS_ACCESS_KEY_ID": "tmp-access",
                "AWS_SECRET_ACCESS_KEY": "tmp-secret",
                "AWS_SESSION_TOKEN": "tmp-session",
                "R2_CREDENTIAL_EXPIRES_AT": "2026-05-28T13:00:00Z",
            }

        def cleanup_pod_artifacts(self, pods: list[dict[str, Any]]) -> list[Any]:
            raise AssertionError("launch must not clean up staging artifacts")

    env = launch._env_for_pod(
        _capability(),
        _provider(),
        staging_store=FakeStagingStore(),
    )

    assert env["AWS_ACCESS_KEY_ID"] == "tmp-access"
    assert env["AWS_SECRET_ACCESS_KEY"] == "tmp-secret"
    assert env["AWS_SESSION_TOKEN"] == "tmp-session"
    assert env["R2_CREDENTIAL_EXPIRES_AT"] == "2026-05-28T13:00:00Z"
    assert "R2_ACCESS_KEY" not in env
    assert "R2_SECRET_KEY" not in env


def test_max_cost_per_hr_reads_provider_constraints() -> None:
    provider = _provider(
        {
            "image_ref": "ghcr.io/acme/pitwall-worker:qwen3",
            "template_name": "pitwall-qwen3-h100",
            "gpu_type_priority": ["NVIDIA H100 80GB HBM3"],
            "constraints": {"max_cost_per_hr": "1.25"},
        }
    )

    assert launch._max_cost_per_hr(provider) == 1.25


def test_provider_attach_timeout_reads_provider_constraints() -> None:
    provider = _provider(
        {
            "image_ref": "ghcr.io/acme/pitwall-worker:qwen3",
            "template_name": "pitwall-qwen3-h100",
            "gpu_type_priority": ["NVIDIA H100 80GB HBM3"],
            "constraints": {"max_attach_hang_s": "42"},
        }
    )

    assert launch._provider_attach_timeout_s(provider) == 42.0


class _FakeLeaseAcquire:
    def __init__(self, conn: _FakeLeaseConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeLeaseConnection:
        return self._conn

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _FakeLeaseConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.rows: dict[str, dict[str, Any]] = {}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.queries.append(query)
        lease_id = str(args[0])
        if lease_id in self.rows and "ON CONFLICT (id) DO UPDATE" not in query:
            raise RuntimeError("duplicate lease id")
        row = {
            "id": lease_id,
            "provider_id": args[1],
            "runpod_pod_id": args[2],
            "state": args[3],
            "created_at": args[4],
            "expires_at": args[5],
            "renewal_policy": args[6],
            "auto_teardown_on_expiry": args[7],
            "endpoints": None,
            "readiness": None,
            "cost_accrued_usd": args[10],
            "last_health_at": args[11],
            "terminated_at": None,
            "terminated_reason": None,
        }
        self.rows[lease_id] = row
        return row


class _FakeLeasePool:
    def __init__(self, conn: _FakeLeaseConnection) -> None:
        self._conn = conn

    def acquire(self) -> _FakeLeaseAcquire:
        return _FakeLeaseAcquire(self._conn)


@pytest.mark.anyio
async def test_pre_lease_persist_callback_upserts_retried_lease_id() -> None:
    conn = _FakeLeaseConnection()
    created_at = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    expiry = datetime(2026, 5, 28, 14, 0, tzinfo=UTC)
    callback = launch._make_pre_lease_persist_callback(
        pool=_FakeLeasePool(conn),
        loop=asyncio.get_running_loop(),
        lease_id="lease_retry",
        provider_id="prov_qwen3_h100",
        created_at=created_at,
        expiry=expiry,
        planned_endpoints=None,
    )

    await asyncio.to_thread(callback, {"id": "pod-first"})
    await asyncio.to_thread(callback, {"id": "pod-retry"})

    assert conn.rows["lease_retry"]["runpod_pod_id"] == "pod-retry"
    assert len(conn.queries) == 2
    assert all("ON CONFLICT (id) DO UPDATE" in query for query in conn.queries)


@pytest.mark.anyio
async def test_ensure_launch_template_uses_provider_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_ensure_template(pool: object, image_ref: str, **kwargs: Any) -> str:
        calls.append({"pool": pool, "image_ref": image_ref, **kwargs})
        return "template-abc123"

    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)
    monkeypatch.setattr(
        launch,
        "get_registry_auth_id_from_env",
        lambda image_ref: f"auth-for-{image_ref}",
    )
    pool = object()

    template = await launch.ensure_launch_template(pool, _capability(), _provider())

    assert template.template_id == "template-abc123"
    assert template.template_name == "pitwall-qwen3-h100"
    assert template.image_ref == "ghcr.io/acme/pitwall-worker:qwen3"
    assert template.registry_auth_id == "auth-for-ghcr.io/acme/pitwall-worker:qwen3"
    assert template.container_disk_gb == 80
    assert template.volume_mount_path == "/workspace"
    assert calls == [
        {
            "pool": pool,
            "image_ref": "ghcr.io/acme/pitwall-worker:qwen3",
            "template_name": "pitwall-qwen3-h100",
            "registry_auth_id": "auth-for-ghcr.io/acme/pitwall-worker:qwen3",
            "container_disk_gb": 80,
            "volume_mount_path": "/workspace",
        }
    ]


@pytest.mark.anyio
async def test_ensure_launch_template_does_not_require_gpu_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ensure_template(*_args: Any, **_kwargs: Any) -> str:
        return "template-image-only"

    provider = _provider(
        {
            "image_ref": "ghcr.io/acme/pitwall-worker:image-only",
            "template_name": "pitwall-image-only",
        }
    )
    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)

    template = await launch.ensure_launch_template(object(), _capability(), provider)

    assert template.template_id == "template-image-only"
    assert template.template_name == "pitwall-image-only"
    assert template.container_disk_gb == 50


@pytest.mark.anyio
async def test_run_launch_dry_run_returns_template_without_creating_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ensure_template(*_args: Any, **_kwargs: Any) -> str:
        return "template-dryrun"

    async def fail_create_pod(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("dry_run must not create a pod")

    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)
    monkeypatch.setattr(launch, "create_pod_with_fallback", fail_create_pod)

    result = await launch.run_launch(
        pool=object(),
        capability=_capability(),
        provider=_provider(),
        request_id="req_dry",
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["pod_id"] is None
    assert result["template_id"] == "template-dryrun"
    assert result["template_name"] == "pitwall-qwen3-h100"
    assert result["capability"] == "llm.qwen3-32b"
    assert result["provider"] == "qwen3-h100-pod-us-ca"
    assert result["network_volume_id"] == "vol-model-cache"
    assert result["data_center_id"] == "US-CA-2"


@pytest.mark.anyio
async def test_run_launch_admits_budget_then_template_then_sync_pod_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    budget_kwargs: dict[str, Any] = {}

    class FakeBudgetGate:
        async def try_launch(self, **kwargs: Any) -> str:
            calls.append("budget_gate")
            budget_kwargs.update(kwargs)
            return "wkl_lease_order"

    async def fake_ensure_template(pool: object, image_ref: str, **kwargs: Any) -> str:
        assert calls == ["budget_gate"]
        calls.append("templates.ensure_template")
        assert image_ref == "ghcr.io/acme/pitwall-worker:qwen3"
        assert kwargs["template_name"] == "pitwall-qwen3-h100"
        return "template-order"

    def fake_create_pod_with_fallback_sync(**kwargs: Any) -> dict[str, Any]:
        assert calls == ["budget_gate", "templates.ensure_template"]
        calls.append("create_pod_with_fallback_sync")
        assert kwargs["template_id"] == "template-order"
        assert kwargs["image_name"] == "ghcr.io/acme/pitwall-worker:qwen3"
        return {"id": "pod-order", "name": kwargs["name"]}

    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)
    monkeypatch.setattr(
        pods,
        "create_pod_with_fallback_sync",
        fake_create_pod_with_fallback_sync,
    )

    result = await launch.run_launch(
        pool=object(),
        capability=_capability(),
        provider=_provider(),
        request_id="req_order",
        budget_gate=FakeBudgetGate(),
    )

    assert calls == [
        "budget_gate",
        "templates.ensure_template",
        "create_pod_with_fallback_sync",
    ]
    assert budget_kwargs == {
        "capability_id": "cap_llm_qwen3",
        "provider_id": "prov_qwen3_h100",
        "estimate_usd": Decimal("0.120000"),
        "workload_type": "inference",
        "idempotency_key": None,
    }
    assert result["workload_id"] == "wkl_lease_order"
    assert result["template_id"] == "template-order"
    assert result["pod_id"] == "pod-order"


@pytest.mark.anyio
async def test_run_launch_persists_ready_pod_readiness_before_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []

    class FakePool:
        def acquire(self) -> object:
            raise AssertionError("fake repository should not acquire directly")

    class FakeBudgetGate:
        async def try_launch(self, **_kwargs: Any) -> str:
            return "wkl_ready"

    class FakeLeaseRepository:
        def __init__(self, pool: object) -> None:
            assert isinstance(pool, FakePool)

        async def update_state(self, lease_id: str, state: str) -> object:
            events.append(("state", state))
            return object()

        async def update_readiness(self, lease_id: str, readiness: object) -> object:
            events.append(("readiness", readiness))
            return object()

    async def fake_ensure_template(*_args: Any, **_kwargs: Any) -> str:
        return "template-ready"

    async def fake_create_pod_with_fallback(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["pre_readiness_callback"] is not None
        return {
            "id": "pod-ready",
            "name": kwargs["name"],
            "readiness": {
                "runtime_seen_at": "2026-05-26T14:00:18Z",
                "port_mappings_seen_at": "2026-05-26T14:00:19Z",
                "probe_passed_at": "2026-05-26T14:00:34Z",
                "probe_method": "ssh_localhost",
            },
        }

    monkeypatch.setattr(launch, "LeaseRepository", FakeLeaseRepository)
    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)
    monkeypatch.setattr(launch, "create_pod_with_fallback", fake_create_pod_with_fallback)

    result = await launch.run_launch(
        pool=FakePool(),
        capability=_capability(),
        provider=_provider(),
        request_id="req_ready",
        budget_gate=FakeBudgetGate(),
    )

    readiness_event = events[2]
    assert result["pod_id"] == "pod-ready"
    assert [event[1] for event in events if event[0] == "state"] == [
        "waiting_runtime",
        "waiting_probe",
        "active",
    ]
    assert readiness_event[0] == "readiness"
    assert readiness_event[1].has_active_signals
    assert readiness_event[1].probe_method == "ssh_localhost"


@pytest.mark.anyio
async def test_run_launch_returns_provider_fallback_signal_for_pre_wait_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBudgetGate:
        async def try_launch(self, **_kwargs: Any) -> str:
            return "wkl_pre_wait_guard"

    async def fake_ensure_template(*_args: Any, **_kwargs: Any) -> str:
        return "template-prewait"

    async def fake_create_pod_with_fallback(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["max_cost_per_hr"] == 1.25
        raise pods.ProviderFallbackRequested("pod pod-1 allocated zero GPUs")

    provider = _provider(
        {
            "image_ref": "ghcr.io/acme/pitwall-worker:qwen3",
            "template_name": "pitwall-qwen3-h100",
            "gpu_type_priority": ["NVIDIA H100 80GB HBM3"],
            "constraints": {"max_cost_per_hr": "1.25"},
            "cost": {"per_second_active": "0.002"},
        }
    )
    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)
    monkeypatch.setattr(launch, "create_pod_with_fallback", fake_create_pod_with_fallback)

    result = await launch.run_launch(
        pool=object(),
        capability=_capability(),
        provider=provider,
        request_id="req_prewait",
        budget_gate=FakeBudgetGate(),
    )

    assert result["provider_fallback"] is True
    assert result["provider_fallback_reason"] == "pod pod-1 allocated zero GPUs"
    assert result["pod_id"] is None
    assert result["lease_id"] is None
    assert result["workload_id"] == "wkl_pre_wait_guard"
    assert result["provider_id"] == "prov_qwen3_h100"


@pytest.mark.anyio
async def test_run_launch_cools_provider_after_volume_attach_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched: list[tuple[str, dict[str, Any]]] = []
    now = datetime(2026, 5, 28, 12, 30, tzinfo=UTC)

    class FakePool:
        def acquire(self) -> object:
            raise AssertionError("fake provider repository should not acquire directly")

    class FakeBudgetGate:
        async def try_launch(self, **_kwargs: Any) -> str:
            return "wkl_attach_hang"

    class FakeProviderRepository:
        def __init__(self, pool: object) -> None:
            assert isinstance(pool, FakePool)

        async def patch(self, provider_id: str, **kwargs: Any) -> object:
            patched.append((provider_id, kwargs))
            return object()

    async def fake_ensure_template(*_args: Any, **_kwargs: Any) -> str:
        return "template-attach"

    async def fake_create_pod_with_fallback(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["volume_attach_timeout_s"] == 42.0
        raise pods.ProviderAttachHangRecoveryRequested(
            "pod pod-hung volume attach hang exceeded 42s (uptimeInSeconds=0)",
            pod_id="pod-hung",
            attach_timeout_s=42.0,
        )

    provider = _provider(
        {
            "image_ref": "ghcr.io/acme/pitwall-worker:qwen3",
            "template_name": "pitwall-qwen3-h100",
            "gpu_type_priority": ["NVIDIA H100 80GB HBM3"],
            "volume_id": "vol-model-cache",
            "constraints": {"max_attach_hang_s": "42"},
            "cost": {"per_second_active": "0.002"},
        }
    )
    monkeypatch.setattr(launch, "ProviderRepository", FakeProviderRepository)
    monkeypatch.setattr(launch, "_utc_now", lambda: now)
    monkeypatch.setattr(launch, "ensure_template", fake_ensure_template)
    monkeypatch.setattr(launch, "create_pod_with_fallback", fake_create_pod_with_fallback)

    result = await launch.run_launch(
        pool=FakePool(),
        capability=_capability(),
        provider=provider,
        request_id="req_attach_hang",
        budget_gate=FakeBudgetGate(),
    )

    cooldown_until = now + launch.ATTACH_HANG_PROVIDER_COOLDOWN
    assert patched == [
        (
            "prov_qwen3_h100",
            {"cooldown_until": cooldown_until},
        )
    ]
    assert result["provider_fallback"] is True
    assert result["provider_fallback_reason"] == (
        "pod pod-hung volume attach hang exceeded 42s (uptimeInSeconds=0)"
    )
    assert result["provider_cooldown_until"] == cooldown_until.isoformat()
    assert result["pod_id"] is None
    assert result["lease_id"] is None


@pytest.mark.anyio
async def test_run_launch_budget_rejection_skips_template_and_sync_pod_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    snapshot = BudgetSnapshot(
        monthly_budget_usd=Decimal("1.000000"),
        per_request_max_usd=Decimal("1.000000"),
        mtd_spend_usd=Decimal("1.000000"),
        estimate_usd=Decimal("0.120000"),
        budget_remaining_usd=Decimal("0"),
    )

    class RejectingBudgetGate:
        async def try_launch(self, **_kwargs: Any) -> str:
            calls.append("budget_gate")
            raise BudgetRejected("monthly_budget", snapshot)

    async def fail_ensure_template(*_args: Any, **_kwargs: Any) -> str:
        calls.append("templates.ensure_template")
        raise AssertionError("template must not be ensured after budget rejection")

    def fail_create_pod_with_fallback_sync(**_kwargs: Any) -> dict[str, Any]:
        calls.append("create_pod_with_fallback_sync")
        raise AssertionError("pod must not be created after budget rejection")

    monkeypatch.setattr(launch, "ensure_template", fail_ensure_template)
    monkeypatch.setattr(
        pods,
        "create_pod_with_fallback_sync",
        fail_create_pod_with_fallback_sync,
    )

    with pytest.raises(BudgetRejected):
        await launch.run_launch(
            pool=object(),
            capability=_capability(),
            provider=_provider(),
            budget_gate=RejectingBudgetGate(),
        )

    assert calls == ["budget_gate"]


@pytest.mark.anyio
async def test_non_pod_lease_provider_is_rejected() -> None:
    provider = _provider()
    provider.provider_type = ProviderType.SERVERLESS_LB

    with pytest.raises(launch.ProviderNotPodLease):
        await launch.ensure_launch_template(object(), _capability(), provider)
