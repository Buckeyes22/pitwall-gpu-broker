"""release program Task 9 fix: the pre-readiness lease-persist callback must run on the
pool's owning event loop.

RunPod pod creation runs in a worker thread (``create_pod_with_fallback`` ->
``asyncio.to_thread(create_pod_with_fallback_sync, ...)``), and
``create_pod_with_fallback_sync`` invokes ``pre_readiness_callback`` from inside
that thread. The callback persists the initial lease row so a crash during the
(long) readiness wait still leaves a DB record to reconcile/teardown — i.e. it
is leak-safety, and must happen *before* the readiness wait.

The original implementation used ``asyncio.run(_persist_lease())``, which spins
up a *fresh* event loop in the worker thread. asyncpg pools are bound to the
loop that created them, so touching the launch pool from that fresh loop raises
``ConnectionDoesNotExistError`` (observed live: a real pod was created, then the
launch aborted at persist). This test reproduces that cross-loop invocation with
a real pool and asserts the lease is persisted.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from pitwall.api.leases.launch import _make_pre_lease_persist_callback
from pitwall.core.enums import LeaseState
from pitwall.db.repository import LeaseRepository
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, requires_pg]


async def test_pre_lease_persist_callback_runs_on_owning_loop(pg_pool) -> None:
    loop = asyncio.get_running_loop()
    created_at = dt.datetime.now(dt.UTC)
    expiry = created_at + dt.timedelta(hours=1)
    lease_id = "lease_persist_loop_test"

    callback = _make_pre_lease_persist_callback(
        pool=pg_pool,
        loop=loop,
        lease_id=lease_id,
        provider_id="prov_persist_loop_test",
        created_at=created_at,
        expiry=expiry,
        planned_endpoints=None,
    )

    # Reproduce production: the callback fires from the worker thread that
    # create_pod_with_fallback_sync runs in (via asyncio.to_thread), while the
    # pool's owning loop is `loop`.
    await asyncio.to_thread(callback, {"id": "pod_persist_loop_test"})

    lease = await LeaseRepository(pg_pool).get(lease_id)
    assert lease is not None
    assert lease.runpod_pod_id == "pod_persist_loop_test"
    assert lease.state == LeaseState.CREATING
    assert lease.provider_id == "prov_persist_loop_test"
