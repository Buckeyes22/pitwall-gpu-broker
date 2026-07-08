"""Handler latency tests for the FastAPI test client path.

Measure handler duration around the FastAPI test client and assert p95 below 50ms.
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest


def _env_for_app(**overrides: str) -> dict[str, str]:
    base: dict[str, str] = {
        "RUNPOD_API_KEY": "test-key",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "REDIS_URL": "redis://localhost:6379/0",
    }
    base.update(overrides)
    return base


def _import_app(env: dict[str, str]):
    old = os.environ.copy()
    os.environ.update(env)
    for k in list(os.environ):
        if k not in env and k in (
            "RUNPOD_API_KEY",
            "DATABASE_URL",
            "REDIS_URL",
            "PITWALL_ADMIN_SECRET",
            "PITWALL_API_TOKEN",
            "PITWALL_INBOUND_RATE_LIMIT",
        ):
            del os.environ[k]
    try:
        mod = importlib.import_module("pitwall.api.app")
        return mod
    finally:
        os.environ.clear()
        os.environ.update(old)


def _calculate_p95(latencies: list[float]) -> float:
    if not latencies:
        return 0.0
    sorted_latencies = sorted(latencies)
    index = int(len(sorted_latencies) * 0.95) - 1
    index = max(0, min(index, len(sorted_latencies) - 1))
    return sorted_latencies[index]


@pytest.fixture(autouse=True)
def _clear_app_module():
    to_remove = [k for k in sys.modules if k.startswith("pitwall.api")]
    for k in to_remove:
        del sys.modules[k]
    yield
    to_remove = [k for k in sys.modules if k.startswith("pitwall.api")]
    for k in to_remove:
        del sys.modules[k]


@pytest.fixture
def app_mod() -> Any:
    env = _env_for_app()
    mod = _import_app(env)
    mod.app.state.pool = MagicMock()
    return mod


@pytest.mark.anyio
async def test_healthz_handler_latency_p95_below_50ms(app_mod: Any) -> None:
    """Handler duration for /healthz endpoint has p95 < 50ms."""
    num_requests = 100
    latencies: list[float] = []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_mod.app),
        base_url="http://test",
    ) as client:
        for _ in range(num_requests):
            start = time.perf_counter()
            response = await client.get("/healthz")
            end = time.perf_counter()
            assert response.status_code == 200
            latencies.append((end - start) * 1000)

    p95 = _calculate_p95(latencies)
    avg = sum(latencies) / len(latencies)
    max_latency = max(latencies)
    min_latency = min(latencies)

    assert p95 < 50, (
        f"p95 latency {p95:.2f}ms exceeded 50ms threshold (avg={avg:.2f}ms, min={min_latency:.2f}ms, max={max_latency:.2f}ms)"
    )


@pytest.mark.anyio
async def test_health_handler_latency_p95_below_50ms(app_mod: Any) -> None:
    """Handler duration for /health endpoint has p95 < 50ms."""
    num_requests = 100
    latencies: list[float] = []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_mod.app),
        base_url="http://test",
    ) as client:
        for _ in range(num_requests):
            start = time.perf_counter()
            response = await client.get("/health")
            end = time.perf_counter()
            assert response.status_code == 200
            latencies.append((end - start) * 1000)

    p95 = _calculate_p95(latencies)
    avg = sum(latencies) / len(latencies)
    max_latency = max(latencies)
    min_latency = min(latencies)

    assert p95 < 50, (
        f"p95 latency {p95:.2f}ms exceeded 50ms threshold (avg={avg:.2f}ms, min={min_latency:.2f}ms, max={max_latency:.2f}ms)"
    )


def _make_client(app_mod: Any) -> Callable[..., Any]:
    async def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_mod.app),
            base_url="http://test",
            **kwargs,
        )

    return client_factory
