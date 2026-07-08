"""Tests for lb_endpoint_hibernate_sweep — daily sweep that does NOT auto-hibernate.

Per the L14 invariant: workersMin > 0 on a hibernated LB endpoint triggers an alert;
the sweep must NOT auto-hibernate (operator decision; alert is the action). The sweep
legitimately reads endpoints and tracks warm duration; the invariant under
test is that it never issues a workersMin-mutating RunPod call.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pitwall.reconciler import _lb_endpoint_hibernate_sweep

pytestmark = pytest.mark.anyio


def _warm_provider() -> dict:
    return {"id": "prov-1", "name": "lb-1", "runpod_endpoint_id": "ep-1"}


class _RunpodReadOnlyClient:
    """Async httpx client stub exposing ONLY GET. An auto-hibernate would require a
    mutating call (PATCH/PUT/POST to set workersMin=0); those are absent here, so any
    such attempt raises AttributeError and fails the test."""

    def __init__(self, *args: object, **kwargs: object) -> None: ...

    async def __aenter__(self) -> _RunpodReadOnlyClient:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def get(self, *args: object, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value={"workersMin": 3})
        return resp


async def test_sweep_runs_without_error() -> None:
    """The sweep must complete without raising even with an empty context."""
    ctx: dict = {}
    await _lb_endpoint_hibernate_sweep(ctx)
    await _lb_endpoint_hibernate_sweep(ctx)
    assert True


async def test_sweep_does_not_auto_hibernate(monkeypatch) -> None:
    """L14: against a warm (workersMin>0) endpoint the sweep must NOT auto-hibernate.

    The read-only client only provides GET; reaching the end of the run proves the
    sweep only reads/tracks and never issued a mutating (hibernating) call.
    """
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)  # no prior obs -> no warm-duration -> no alert
    redis.set = AsyncMock()
    ctx: dict = {"db_pool": MagicMock(), "redis": redis}

    with (
        patch(
            "pitwall.reconciler.fetch_lb_providers_for_hibernate_sweep",
            AsyncMock(return_value=[_warm_provider()]),
        ),
        patch.object(httpx, "AsyncClient", _RunpodReadOnlyClient),
    ):
        await _lb_endpoint_hibernate_sweep(ctx)


async def test_sweep_skips_when_no_pool() -> None:
    """The sweep must not fail when db_pool is absent from context."""
    ctx: dict = {}
    await _lb_endpoint_hibernate_sweep(ctx)
    assert True


async def test_sweep_skips_when_redis_missing(monkeypatch) -> None:
    """The sweep must not fail when redis is absent from context."""
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    ctx: dict = {"db_pool": MagicMock()}  # no "redis" key
    with (
        patch(
            "pitwall.reconciler.fetch_lb_providers_for_hibernate_sweep",
            AsyncMock(return_value=[_warm_provider()]),
        ),
        patch.object(httpx, "AsyncClient", _RunpodReadOnlyClient),
    ):
        await _lb_endpoint_hibernate_sweep(ctx)
