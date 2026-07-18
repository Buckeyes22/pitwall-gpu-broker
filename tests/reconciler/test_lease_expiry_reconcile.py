"""Tests for lease_expiry_reconcile — 60-second lease expiry reconciliation."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pitwall.core.enums import LeaseState
from pitwall.reconciler import _lease_expiry_reconcile

pytestmark = pytest.mark.anyio


def _make_mock_conn() -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    return conn


def _make_mock_pool() -> MagicMock:
    pool = MagicMock()
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=_make_mock_conn())
    acq.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acq)
    return pool


async def test_lease_expiry_skips_when_no_pool() -> None:
    """No action is taken if db_pool is not in ctx."""
    ctx: dict = {}
    await _lease_expiry_reconcile(ctx)
    await _lease_expiry_reconcile(ctx)
    assert True


async def test_lease_expiry_fetches_active_leases() -> None:
    """The reconciler queries for active leases approaching expiry."""
    pool = _make_mock_pool()
    ctx: dict = {"db_pool": pool, "redis": None}
    await _lease_expiry_reconcile(ctx)
    pool.acquire.return_value.__aenter__.return_value.fetch.assert_called_once()


async def test_lease_expiry_fires_warning_at_t15() -> None:
    """A warning event is published when lease is at T-15 threshold."""
    pool = _make_mock_pool()
    now = dt.datetime.now(dt.UTC)
    expires_at = now + dt.timedelta(minutes=14)

    mock_conn = pool.acquire.return_value.__aenter__.return_value
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": "lease-1",
                "provider_id": "provider-1",
                "runpod_pod_id": "pod-1",
                "expires_at": expires_at,
                "auto_teardown_on_expiry": True,
                "state": "active",
            }
        ]
    )

    redis_mock = MagicMock()
    redis_mock.publish = MagicMock(return_value=1)

    ctx: dict = {"db_pool": pool, "redis": redis_mock}

    with patch.dict("os.environ", {"PITWALL_LEASE_ADVANCE_WARNING_MIN": "15,5"}):
        await _lease_expiry_reconcile(ctx)

    redis_mock.publish.assert_called_once()
    call_args = redis_mock.publish.call_args
    channel = call_args[0][0]
    payload = call_args[0][1]
    assert channel == "pitwall.leases.events"
    assert "lease.expiring" in payload
    assert "lease-1" in payload


async def test_lease_expiry_fires_warning_at_t5() -> None:
    """A warning event is published when lease is at T-5 threshold."""
    pool = _make_mock_pool()
    now = dt.datetime.now(dt.UTC)
    expires_at = now + dt.timedelta(minutes=4)

    mock_conn = pool.acquire.return_value.__aenter__.return_value
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": "lease-2",
                "provider_id": "provider-1",
                "runpod_pod_id": "pod-2",
                "expires_at": expires_at,
                "auto_teardown_on_expiry": True,
                "state": "active",
            }
        ]
    )

    redis_mock = MagicMock()
    redis_mock.publish = MagicMock(return_value=1)

    ctx: dict = {"db_pool": pool, "redis": redis_mock}

    with patch.dict("os.environ", {"PITWALL_LEASE_ADVANCE_WARNING_MIN": "15,5"}):
        await _lease_expiry_reconcile(ctx)

    redis_mock.publish.assert_called_once()
    call_args = redis_mock.publish.call_args
    channel = call_args[0][0]
    assert channel == "pitwall.leases.events"


async def test_lease_expiry_tears_down_expired_lease_at_t0(monkeypatch) -> None:
    """At T-0 the reconciler calls scoped teardown with an expired terminal state."""
    pool = _make_mock_pool()
    now = dt.datetime.now(dt.UTC)
    expires_at = now - dt.timedelta(minutes=1)
    teardown_calls: list[dict[str, object]] = []

    mock_conn = pool.acquire.return_value.__aenter__.return_value
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": "lease-expired",
                "provider_id": "provider-1",
                "runpod_pod_id": "pod-expired",
                "expires_at": expires_at,
                "auto_teardown_on_expiry": True,
                "state": "active",
            }
        ]
    )

    redis_mock = MagicMock()

    ctx: dict = {"db_pool": pool, "redis": redis_mock}

    async def fake_run_teardown(
        lease_id: str,
        *,
        pool: object,
        redis_client: object | None,
        reason: str | None,
        now: dt.datetime,
        terminal_state: LeaseState | str,
    ) -> None:
        teardown_calls.append(
            {
                "lease_id": lease_id,
                "pool": pool,
                "redis_client": redis_client,
                "reason": reason,
                "now": now,
                "terminal_state": terminal_state,
            }
        )

    # Import the module object and patch it directly, rather than via the
    # "pitwall.api.leases.teardown.run_teardown" string. pytest resolves a string
    # target by getattr-walking from the top package, which — after another test
    # purges `pitwall.api.*` from sys.modules but leaves the stale parent-package
    # attributes — lands on the OLD module object, while the reconciler's
    # `from pitwall.api.leases.teardown import run_teardown` re-imports a FRESH one
    # (the import system trusts sys.modules, not parent attrs). The two diverge and
    # the patch misses. `import ... as` forces the import machinery to reconcile
    # sys.modules and the parent attr to a single object, which the reconciler then
    # resolves identically — so the patch always lands.
    import pitwall.api.leases.teardown as _teardown_mod

    monkeypatch.setattr(_teardown_mod, "run_teardown", fake_run_teardown)

    with patch.dict("os.environ", {"PITWALL_LEASE_ADVANCE_WARNING_MIN": "15,5"}):
        await _lease_expiry_reconcile(ctx)

    redis_mock.publish.assert_not_called()
    assert len(teardown_calls) == 1
    assert teardown_calls[0]["lease_id"] == "lease-expired"
    assert teardown_calls[0]["pool"] is pool
    assert teardown_calls[0]["redis_client"] is redis_mock
    assert teardown_calls[0]["reason"] == "lease_expired"
    assert teardown_calls[0]["terminal_state"] is LeaseState.EXPIRED
    assert isinstance(teardown_calls[0]["now"], dt.datetime)


async def test_lease_expiry_skips_leases_outside_warning_window() -> None:
    """No warning is fired for leases not yet in any warning window."""
    pool = _make_mock_pool()
    now = dt.datetime.now(dt.UTC)
    expires_at = now + dt.timedelta(minutes=30)

    mock_conn = pool.acquire.return_value.__aenter__.return_value
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": "lease-far",
                "provider_id": "provider-1",
                "runpod_pod_id": "pod-far",
                "expires_at": expires_at,
                "auto_teardown_on_expiry": True,
                "state": "active",
            }
        ]
    )

    redis_mock = MagicMock()

    ctx: dict = {"db_pool": pool, "redis": redis_mock}

    with patch.dict("os.environ", {"PITWALL_LEASE_ADVANCE_WARNING_MIN": "15,5"}):
        await _lease_expiry_reconcile(ctx)

    redis_mock.publish.assert_not_called()


async def test_lease_expiry_uses_default_warning_minutes() -> None:
    """Default warning minutes (15,5) are used when env var is not set."""
    pool = _make_mock_pool()
    now = dt.datetime.now(dt.UTC)
    expires_at = now + dt.timedelta(minutes=14)

    mock_conn = pool.acquire.return_value.__aenter__.return_value
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": "lease-1",
                "provider_id": "provider-1",
                "runpod_pod_id": "pod-1",
                "expires_at": expires_at,
                "auto_teardown_on_expiry": True,
                "state": "active",
            }
        ]
    )

    redis_mock = MagicMock()
    redis_mock.publish = MagicMock(return_value=1)

    ctx: dict = {"db_pool": pool, "redis": redis_mock}

    with patch.dict("os.environ", {}, clear=True):
        await _lease_expiry_reconcile(ctx)

    redis_mock.publish.assert_called_once()
