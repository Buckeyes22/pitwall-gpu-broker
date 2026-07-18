"""Gap-closing tests for reconciler/__init__.py.

Direct tests for the pure helpers and the mock-pool DB helpers that the
existing reconciler async-job tests under-exercise. Imports from
``pitwall.reconciler`` (the package __init__). No real DB/Redis/sleep.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pitwall.reconciler as recon
from pitwall.core.enums import WorkloadState
from tests.conftest import make_asyncpg_pool

TZ_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("redis://localhost:6379/0", True),
        ("redis://127.0.0.1:6380/0", True),
        ("", False),
        ("not-a-url", False),
        ("http://localhost", False),
    ],
    ids=["loopback", "ip", "empty", "garbage", "wrong-scheme"],
)
def test_validate_redis_dsn(dsn: str, expected: bool) -> None:
    assert recon.validate_redis_dsn(dsn) is expected


def test_check_redis_config_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    rc = recon.check_redis_config()
    assert rc == 1
    assert "REDIS_URL is not set" in capsys.readouterr().err


def test_check_redis_config_invalid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REDIS_URL", "not-a-url")
    rc = recon.check_redis_config()
    assert rc == 1
    assert "not a valid redis" in capsys.readouterr().err


def test_check_redis_config_masks_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://user:hunter2@redis.internal:6379/0")
    rc = recon.check_redis_config()
    out = capsys.readouterr().out
    assert rc == 0
    assert "hunter2" not in out
    assert "redis.internal:6379" in out


def test_check_redis_config_valid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    rc = recon.check_redis_config()
    assert rc == 0
    assert "REDIS_URL is valid" in capsys.readouterr().out


async def test_worker_startup_attaches_db_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = object()
    get_pool = AsyncMock(return_value=pool)
    monkeypatch.setattr("pitwall.db.get_pool", get_pool)
    ctx: dict[str, object] = {}

    await recon.WorkerSettings.on_startup(ctx)

    get_pool.assert_awaited_once_with()
    assert ctx["db_pool"] is pool


async def test_worker_startup_raises_when_db_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_pool = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr("pitwall.db.get_pool", get_pool)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await recon.WorkerSettings.on_startup({})


async def test_worker_shutdown_closes_db_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    close_pool = AsyncMock()
    monkeypatch.setattr("pitwall.db.close_pool", close_pool)
    ctx: dict[str, object] = {"db_pool": object()}

    await recon.WorkerSettings.on_shutdown(ctx)

    close_pool.assert_awaited_once_with()
    assert "db_pool" not in ctx


def test_worker_settings_register_webhook_job_with_arq() -> None:
    """arq only reads Worker.__init__ parameter names from the settings class;
    the enqueued webhook job must be exposed as ``functions`` or it is
    silently dropped and every enqueue fails with 'function not found'."""
    from arq.worker import get_kwargs

    kwargs = get_kwargs(recon.WorkerSettings)
    assert recon._process_webhook_terminal_status in kwargs["functions"]
    assert kwargs["cron_jobs"]


@pytest.mark.parametrize(
    ("status", "terminal", "state"),
    [
        ("COMPLETED", True, WorkloadState.COMPLETED),
        ("FAILED", True, WorkloadState.FAILED),
        ("CANCELLED", True, WorkloadState.CANCELLED),
        ("TIMED_OUT", True, WorkloadState.TIMED_OUT),
        ("IN_PROGRESS", False, None),
        ("IN_QUEUE", False, None),
        ("UNKNOWN", False, None),
    ],
    ids=["completed", "failed", "cancelled", "timed_out", "in_progress", "in_queue", "unknown"],
)
def test_map_runpod_status(status: str, terminal: bool, state: WorkloadState | None) -> None:
    result = recon.map_runpod_status(status)
    assert result.terminal is terminal
    assert result.state == state
    if terminal:
        assert result.completed_at is not None


def test_map_runpod_status_computes_cost() -> None:
    result = recon.map_runpod_status(
        "COMPLETED",
        cost_per_hr=Decimal("3.60"),
        worker_time_ms=1_000,
        completed_at=TZ_NOW,
    )
    assert result.actual_cost == Decimal("0.001000")
    assert result.completed_at == TZ_NOW


@pytest.mark.parametrize(
    ("cost_per_hr", "worker_time_ms", "expected"),
    [
        (None, 1000, None),
        (Decimal("3.60"), None, None),
        (Decimal("3.60"), 0, None),
        (Decimal("3.60"), 3_600_000, Decimal("3.600000")),
    ],
    ids=["no-cost", "no-time", "zero-time", "one-hour"],
)
def test_compute_actual_cost(
    cost_per_hr: Decimal | None, worker_time_ms: int | None, expected: Decimal | None
) -> None:
    assert recon._compute_actual_cost(cost_per_hr, worker_time_ms) == expected


def test_build_workload_completed_event_minimal() -> None:
    workload = {
        "id": "wkl_1",
        "capability_id": "cap_1",
        "provider_id": "prov_1",
        "state": WorkloadState.COMPLETED,
        "completed_at": TZ_NOW,
        "execution_ms": 1234,
        "output_bytes": 56,
        "cost_actual_usd": Decimal("0.42"),
    }
    event = recon.build_workload_completed_event(workload)
    assert event["event"] == "workload.completed"
    assert event["workload_id"] == "wkl_1"
    assert event["state"] == "completed"
    assert event["completed_at"] == TZ_NOW.isoformat()
    assert event["cost_actual_usd"] == "0.42"
    assert "error" not in event


def test_build_workload_completed_event_with_optionals() -> None:
    workload = {
        "id": "wkl_2",
        "state": WorkloadState.FAILED,
        "completed_at": None,
        "cost_actual_usd": None,
        "error": "boom",
        "result": {"x": 1},
        "fallback_chain": ["prov_a", "prov_b"],
    }
    event = recon.build_workload_completed_event(workload)
    assert event["completed_at"] is None
    assert event["cost_actual_usd"] is None
    assert event["error"] == "boom"
    assert event["result"] == {"x": 1}
    assert event["fallback_chain"] == ["prov_a", "prov_b"]


@pytest.mark.anyio
async def test_fetch_active_workloads_returns_dicts() -> None:
    pool = make_asyncpg_pool(fetch=[{"id": "wkl_1", "runpod_job_id": "job_1"}])
    rows = await recon.fetch_active_workloads(pool)
    assert rows == [{"id": "wkl_1", "runpod_job_id": "job_1"}]


@pytest.mark.anyio
async def test_apply_terminal_state_updated_true() -> None:
    pool = make_asyncpg_pool(fetch=[{"id": "wkl_1"}])
    updated = await recon.apply_terminal_state(
        pool,
        workload_id="wkl_1",
        state=WorkloadState.COMPLETED,
        actual_cost=Decimal("0.10"),
        completed_at=TZ_NOW,
    )
    assert updated is True
    pool.conn.fetch.assert_awaited_once()


@pytest.mark.anyio
async def test_apply_terminal_state_already_terminal_false() -> None:
    pool = make_asyncpg_pool(fetch=[])
    updated = await recon.apply_terminal_state(
        pool,
        workload_id="wkl_1",
        state=WorkloadState.COMPLETED,
        actual_cost=None,
        completed_at=TZ_NOW,
    )
    assert updated is False


@pytest.mark.anyio
async def test_fetch_workload_by_id_none_when_missing() -> None:
    pool = make_asyncpg_pool(fetchrow=None)
    assert await recon.fetch_workload_by_id(pool, "wkl_missing") is None


@pytest.mark.anyio
async def test_apply_terminal_status_and_publish_no_workload_returns_false() -> None:
    pool = make_asyncpg_pool(fetchrow=None)
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=0)
    result = await recon.apply_terminal_status_and_publish(pool, redis, "job_missing", "COMPLETED")
    assert result is False
    redis.publish.assert_not_called()


@pytest.mark.anyio
async def test_apply_terminal_status_and_publish_non_terminal_returns_false() -> None:
    pool = make_asyncpg_pool(fetchrow={"id": "wkl_1"})
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=0)
    result = await recon.apply_terminal_status_and_publish(pool, redis, "job_1", "IN_PROGRESS")
    assert result is False


@pytest.mark.anyio
async def test_cost_reconcile_noop_without_pool() -> None:
    await recon._cost_reconcile({"redis": None})


@pytest.mark.anyio
async def test_cost_reconcile_empty_active_is_noop() -> None:
    pool = make_asyncpg_pool(fetch=[])
    await recon._cost_reconcile({"db_pool": pool, "redis": None})
    pool.conn.fetch.assert_awaited()


@pytest.mark.anyio
async def test_publish_workload_completed_redis_none_returns_zero() -> None:
    result = await recon.publish_workload_completed(None, {"event": "test"})
    assert result == 0


@pytest.mark.anyio
async def test_publish_workload_completed_success() -> None:
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=1)
    event = {"event": "workload.completed", "workload_id": "wkl_1"}
    result = await recon.publish_workload_completed(redis, event)
    assert result == 1


@pytest.mark.anyio
async def test_publish_workload_completed_exception_returns_zero() -> None:
    redis = MagicMock()
    redis.publish = MagicMock(side_effect=RuntimeError("boom"))
    event = {"event": "workload.completed", "workload_id": "wkl_1"}
    result = await recon.publish_workload_completed(redis, event)
    assert result == 0


@pytest.mark.anyio
async def test_apply_terminal_status_and_publish_terminal_publishes() -> None:
    wl_row = {
        "id": "wkl_1",
        "capability_id": "c",
        "provider_id": "p",
        "state": "queued",
        "runpod_job_id": "job_1",
        "completed_at": TZ_NOW,
        "execution_ms": 100,
        "output_bytes": 10,
        "cost_actual_usd": Decimal("0.01"),
        "error": None,
        "result": None,
        "fallback_chain": None,
    }
    pool = make_asyncpg_pool(fetch=[wl_row])
    pool.conn.fetchrow = AsyncMock(side_effect=[wl_row, wl_row])
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=1)
    result = await recon.apply_terminal_status_and_publish(pool, redis, "job_1", "COMPLETED")
    assert result is True


@pytest.mark.anyio
async def test_aggregate_daily_cost_calls_execute() -> None:
    pool = make_asyncpg_pool(execute="INSERT 0 1")
    await recon.aggregate_daily_cost(pool)
    pool.conn.execute.assert_awaited()


@pytest.mark.anyio
async def test_fetch_providers_for_health_probe() -> None:
    pool = make_asyncpg_pool(
        fetch=[
            {
                "id": "p1",
                "name": "prov",
                "provider_type": "serverless_lb",
                "runpod_endpoint_id": "ep1",
                "health_status": "healthy",
                "consecutive_failures": 0,
                "cooldown_trips": 0,
                "cooldown_until": None,
            }
        ]
    )
    rows = await recon.fetch_providers_for_health_probe(pool)
    assert len(rows) == 1
    assert rows[0]["id"] == "p1"


@pytest.mark.anyio
async def test_fetch_lb_providers_for_hibernate_sweep() -> None:
    pool = make_asyncpg_pool(
        fetch=[
            {
                "id": "p1",
                "name": "prov",
                "provider_type": "serverless_lb",
                "runpod_endpoint_id": "ep1",
                "config": {},
            }
        ]
    )
    rows = await recon.fetch_lb_providers_for_hibernate_sweep(pool)
    assert len(rows) == 1


@pytest.mark.anyio
async def test_update_provider_health() -> None:
    pool = make_asyncpg_pool(execute="UPDATE 1")
    await recon.update_provider_health(
        pool,
        provider_id="prov_1",
        health_status="healthy",
        consecutive_failures=0,
        cooldown_trips=0,
        cooldown_until=None,
    )
    pool.conn.execute.assert_awaited()


@pytest.mark.anyio
async def test_cost_reconcile_with_terminal_workload() -> None:
    pool = make_asyncpg_pool(fetch=[{"id": "wkl_1", "runpod_job_id": "job_1"}])
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=0)
    await recon._cost_reconcile({"db_pool": pool, "redis": redis})
    pool.conn.fetch.assert_awaited()


@pytest.mark.anyio
async def test_process_webhook_terminal_status() -> None:
    pool = make_asyncpg_pool(fetchrow=None)
    result = await recon._process_webhook_terminal_status(
        {"db_pool": pool, "redis": None}, "job_1", "COMPLETED"
    )
    assert result is None


@pytest.mark.anyio
async def test_process_webhook_terminal_status_no_pool() -> None:
    result = await recon._process_webhook_terminal_status({"redis": None}, "job_1", "COMPLETED")
    assert result is None


@pytest.mark.anyio
async def test_dispatch_workload_completion_webhooks_records_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64
    import json
    from types import SimpleNamespace

    import pitwall.db.repository as repository
    import pitwall.webhook_dispatcher as webhook_dispatcher

    monkeypatch.setenv(
        "PITWALL_WEBHOOK_ENCRYPTION_KEYS",
        json.dumps({"v1": base64.urlsafe_b64encode(bytes(range(32))).decode()}),
    )
    monkeypatch.setenv("PITWALL_WEBHOOK_ENCRYPTION_CURRENT_KEY", "v1")
    subscription = SimpleNamespace(
        id="7",
        webhook_url="https://hooks.example.test/events",
        hmac_secret="signing-secret",
    )
    monkeypatch.setattr(
        repository.WebhookSubscriptionRepository,
        "list_for_dispatch",
        AsyncMock(return_value=[subscription]),
    )
    insert_failure = AsyncMock()
    monkeypatch.setattr(repository.WebhookDeliveryFailureRepository, "insert", insert_failure)
    monkeypatch.setattr(
        webhook_dispatcher,
        "dispatch_completion",
        AsyncMock(
            return_value={
                "7": {
                    "success": False,
                    "attempt": 4,
                    "status_code": 503,
                    "error_message": "Retryable HTTP status: 503",
                    "delivery_id": "delivery-7",
                    "state": "terminal_failure",
                }
            }
        ),
    )

    results = await recon.dispatch_workload_completion_webhooks(
        make_asyncpg_pool(),
        {"id": "wkl_7", "capability_id": "cap_7", "state": "completed"},
        {"event": "workload.completed", "workload_id": "wkl_7"},
    )

    assert results["7"]["state"] == "terminal_failure"
    insert_failure.assert_awaited_once()
    stored_payload = insert_failure.await_args.args[3]
    assert stored_payload == {
        "event": "workload.completed",
        "workload_id": "wkl_7",
        "delivery_id": "delivery-7",
        "state": "completed",
    }


@pytest.mark.anyio
async def test_poll_and_reconcile_no_api_key_returns_early(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "")
    pool = make_asyncpg_pool(fetch=[])
    await recon._poll_and_reconcile({"db_pool": pool, "redis": None})
    pool.conn.fetch.assert_not_called()


@pytest.mark.anyio
async def test_poll_and_reconcile_missing_endpoint_id_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    workload_row = {
        "id": "wkl_1",
        "runpod_job_id": "job_1",
        "provider_id": "prov_1",
        "runpod_endpoint_id": None,
        "provider_type": "serverless_queue",
    }
    pool = make_asyncpg_pool(fetch=[workload_row])
    redis = MagicMock()
    await recon._poll_and_reconcile({"db_pool": pool, "redis": redis})
    pool.conn.fetch.assert_awaited()


@pytest.mark.anyio
async def test_idempotency_gc_no_pool() -> None:
    await recon._idempotency_gc({"redis": None})


@pytest.mark.anyio
async def test_idempotency_gc_with_pool() -> None:
    pool = make_asyncpg_pool(execute="DELETE 0")
    await recon._idempotency_gc({"db_pool": pool, "redis": None})
    pool.conn.execute.assert_awaited()


@pytest.mark.anyio
async def test_health_probe_no_api_key_returns_early(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "")
    pool = make_asyncpg_pool(fetch=[])
    await recon._health_probe({"db_pool": pool, "redis": None})
    pool.conn.fetch.assert_not_called()


@pytest.mark.anyio
async def test_health_probe_no_pool_returns_early() -> None:
    await recon._health_probe({"redis": None})


@pytest.mark.anyio
async def test_health_probe_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    prov_row = {
        "id": "prov_1",
        "name": "lb1",
        "provider_type": "serverless_lb",
        "runpod_endpoint_id": "ep_1",
        "health_status": "healthy",
        "consecutive_failures": 0,
        "cooldown_trips": 0,
        "cooldown_until": None,
    }
    pool = make_asyncpg_pool(fetch=[prov_row])
    pool.conn.execute = AsyncMock(return_value="UPDATE 1")
    redis = MagicMock()

    class MockProbeResult:
        healthy = True

    class MockLBClient:
        def __init__(self, api_key):
            pass

        async def probe(self, endpoint_id):
            return MockProbeResult()

    with (
        patch("pitwall.runpod_client.lb.LBClient", MockLBClient),
        patch("pitwall.reconciler.is_in_cooldown", return_value=False),
        patch("pitwall.reconciler.apply_probe_result") as mock_apply,
    ):
        mock_apply.return_value = type(
            "obj",
            (object,),
            {
                "health_status": "healthy",
                "consecutive_failures": 0,
                "cooldown_trips": 0,
                "cooldown_until": None,
            },
        )()
        await recon._health_probe({"db_pool": pool, "redis": redis})
        pool.conn.execute.assert_awaited()


@pytest.mark.anyio
async def test_health_probe_in_cooldown_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    prov_row = {
        "id": "prov_1",
        "name": "lb1",
        "provider_type": "serverless_lb",
        "runpod_endpoint_id": "ep_1",
        "health_status": "healthy",
        "consecutive_failures": 0,
        "cooldown_trips": 0,
        "cooldown_until": None,
    }
    pool = make_asyncpg_pool(fetch=[prov_row])
    redis = MagicMock()

    with patch("pitwall.reconciler.is_in_cooldown", return_value=True):
        await recon._health_probe({"db_pool": pool, "redis": redis})
        pool.conn.execute.assert_not_called()


@pytest.mark.anyio
async def test_poll_and_reconcile_with_queue_provider_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    workload_row = {
        "id": "wkl_1",
        "runpod_job_id": "job_1",
        "provider_id": "prov_1",
        "runpod_endpoint_id": "ep_1",
        "provider_type": "serverless_queue",
    }
    pool = make_asyncpg_pool(fetch=[workload_row])
    redis = MagicMock()
    pool.conn.fetch = AsyncMock(return_value=[])

    class MockQueueJob:
        status = "COMPLETED"

    class MockQueueClient:
        def __init__(self, api_key):
            pass

        async def status(self, endpoint_id, job_id):
            return MockQueueJob()

    with patch("pitwall.runpod_client.queue.QueueClient", MockQueueClient):
        await recon._poll_and_reconcile({"db_pool": pool, "redis": redis})
        pool.conn.fetch.assert_awaited()


@pytest.mark.anyio
async def test_lease_expiry_reconcile_no_pool_returns_early() -> None:
    await recon._lease_expiry_reconcile({"redis": None})


@pytest.mark.anyio
async def test_lease_expiry_reconcile_with_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    lease_row = {
        "id": "lease_1",
        "provider_id": "prov_1",
        "runpod_pod_id": "pod_1",
        "expires_at": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10),
        "auto_teardown_on_expiry": True,
        "state": "active",
    }
    pool = make_asyncpg_pool(fetch=[lease_row])
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=1)
    await recon._lease_expiry_reconcile({"db_pool": pool, "redis": redis})
    pool.conn.fetch.assert_awaited()


@pytest.mark.anyio
async def test_lease_expiry_reconcile_expired_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    lease_row = {
        "id": "lease_1",
        "provider_id": "prov_1",
        "runpod_pod_id": "pod_1",
        "expires_at": dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
        "auto_teardown_on_expiry": True,
        "state": "active",
    }
    pool = make_asyncpg_pool(fetch=[lease_row])
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=1)
    with patch("pitwall.api.leases.teardown.run_teardown", new_callable=AsyncMock) as mock_teardown:
        await recon._lease_expiry_reconcile({"db_pool": pool, "redis": redis})
        mock_teardown.assert_awaited_once()
    pool.conn.fetch.assert_awaited()


@pytest.mark.anyio
async def test_lb_endpoint_hibernate_sweep_no_providers_returns_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    pool = make_asyncpg_pool(fetch=[])
    redis = MagicMock()
    await recon._lb_endpoint_hibernate_sweep({"db_pool": pool, "redis": redis})
    pool.conn.fetch.assert_awaited()


@pytest.mark.anyio
async def test_lb_endpoint_hibernate_sweep_no_api_key_returns_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "")
    pool = make_asyncpg_pool(
        fetch=[
            {
                "id": "p1",
                "name": "prov",
                "provider_type": "serverless_lb",
                "runpod_endpoint_id": "ep1",
                "config": {},
            }
        ]
    )
    redis = MagicMock()
    await recon._lb_endpoint_hibernate_sweep({"db_pool": pool, "redis": redis})
    pool.conn.fetch.assert_not_called()


@pytest.mark.anyio
async def test_lb_endpoint_hibernate_sweep_fetch_raises_returns_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    pool = make_asyncpg_pool(fetch=[])
    pool.conn.fetch = AsyncMock(side_effect=RuntimeError("boom"))
    redis = MagicMock()
    await recon._lb_endpoint_hibernate_sweep({"db_pool": pool, "redis": redis})
    pool.conn.fetch.assert_awaited()


@pytest.mark.anyio
async def test_publish_lease_warning_redis_none() -> None:
    result = await recon._publish_lease_warning(
        None,
        lease_id="l1",
        provider_id="p1",
        runpod_pod_id="pod_1",
        minutes_until_expiry=10,
        warning_threshold=15,
    )
    assert result is None


@pytest.mark.anyio
async def test_publish_lease_warning_success() -> None:
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=1)
    await recon._publish_lease_warning(
        redis,
        lease_id="l1",
        provider_id="p1",
        runpod_pod_id="pod_1",
        minutes_until_expiry=10,
        warning_threshold=15,
    )
    redis.publish.assert_awaited()


@pytest.mark.anyio
async def test_publish_lease_warning_exception_suppressed() -> None:
    redis = MagicMock()
    redis.publish = MagicMock(side_effect=RuntimeError("boom"))
    result = await recon._publish_lease_warning(
        redis,
        lease_id="l1",
        provider_id="p1",
        runpod_pod_id="pod_1",
        minutes_until_expiry=10,
        warning_threshold=15,
    )
    assert result is None


@pytest.mark.anyio
async def test_backup_drill_no_pool() -> None:
    await recon._backup_drill({"redis": None})


@pytest.mark.anyio
async def test_archive_old_workloads_no_pool() -> None:
    await recon._archive_old_workloads({"redis": None})


@pytest.mark.anyio
async def test_archive_old_workloads_no_archive_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PITWALL_ARCHIVE_DIR", raising=False)
    pool = make_asyncpg_pool(fetch=[])
    await recon._archive_old_workloads({"db_pool": pool, "redis": None})
    pool.conn.fetch.assert_not_called()


@pytest.mark.anyio
async def test_rollup_job_no_pool() -> None:
    await recon._rollup_job({"redis": None})


@pytest.mark.anyio
async def test_rollup_job_no_redis() -> None:
    pool = make_asyncpg_pool(fetch=[])
    await recon._rollup_job({"db_pool": pool, "redis": None})
    pool.conn.fetch.assert_not_called()


def test_build_workload_completed_event_state_is_enum_value() -> None:
    workload = {
        "id": "wkl_1",
        "capability_id": "cap_1",
        "provider_id": "prov_1",
        "state": WorkloadState.COMPLETED,
        "completed_at": TZ_NOW,
        "execution_ms": 100,
        "output_bytes": 10,
        "cost_actual_usd": Decimal("0.01"),
    }
    event = recon.build_workload_completed_event(workload)
    assert event["state"] == "completed"
