"""Shared pytest fixtures for Pitwall's hermetic HTTP tests.

Live-call gate: tests that call real RunPod APIs should be marked with
``@pytest.mark.live`` and will be skipped unless ``RUNPOD_LIVE=1`` is set
or ``--run-live`` is passed to pytest. This prevents accidental live calls
in the hermetic test suite.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib
import os
import sys
import warnings
from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, ProviderType
from pitwall.core.models import Capability, Provider
from tests.fakes.mcp import FakeServiceLayerRecorder
from tests.fakes.runpod import (
    FakePodStateMachine,
    FakeRunPodTemplateSdk,
    RunPodBillingFake,
    RunPodLBFake,
    RunPodResponseFactory,
    RunPodRestFake,
    RunPodServerlessFake,
    RunPodTemplateFake,
)

# Hermetic env defaults — set at MODULE level (not in a fixture) so they apply
# BEFORE pytest collection imports test modules. Several pitwall packages call
# require_runtime_env(...) at import time (mcp was moved behind a function, but
# api/reconciler/webhook/cost-exporter still validate at import); without these
# a bare ``pytest`` with no env would SystemExit(78) during collection.
# ``setdefault`` never overrides a real value exported by a contributor or CI.
os.environ.setdefault("RUNPOD_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://pitwall:pitwall@127.0.0.1:5444/pitwall_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


RUN_LIVE_ENV_VARS = ("RUNPOD_LIVE", "PITWALL_RUN_LIVE")
RUN_LIVE_OPTION = "--run-live"
TEST_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)

# Test modules that open a real asyncpg connection (DATABASE_URL) with no skip
# guard. Auto-marked `integration` in pytest_collection_modifyitems so the fast
# suite never tries to reach a live Postgres (release program converts these to pg_pool).
_REAL_DB_TEST_MODULES = (
    "tests/db/test_repository.py",
    "tests/db/test_migration_indexes.py",
    "tests/db/test_cover_duplicate_rejection.py",
    "tests/test_webhook_duplicate_delivery_stress.py",
)

RequestHandler = Callable[[httpx.Request], httpx.Response]
AsyncpgPoolFactory = Callable[..., MagicMock]
RowFactory = Callable[..., dict[str, Any]]


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Clear the process-global settings cache around every test.

    ``pitwall.config.get_settings`` is an ``lru_cache``; combined with tests that
    reload modules via ``del sys.modules`` + ``os.environ`` mutation, a stale
    cached ``PitwallSettings`` can otherwise leak across tests and make unrelated
    tests fail only in the full suite (the serverless_lb / langfuse orphaning
    class). Clearing before and after each test makes that class impossible.
    """
    from pitwall.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_capability_row(
    *,
    id: str = "cap_embedding_bge_m3",
    name: str = "embedding.bge-m3",
    version: str = "1.0.0",
    class_: str = "embedding",
    description: str | None = "BGE-M3 multilingual embedding model",
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    defaults: dict[str, Any] | None = None,
    cost_mode: str = "per_second",
    hints_supported: list[str] | None = None,
    source: str = "api",
    last_applied_yaml_hash: str | None = None,
    enabled: bool = True,
    created_at: dt.datetime = TEST_NOW,
    updated_at: dt.datetime = TEST_NOW,
    config_overrides: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Build a dict-shaped asyncpg capability row for hermetic repository tests."""
    config: dict[str, Any] = {
        "description": description,
        "input_schema": input_schema
        if input_schema is not None
        else {
            "type": "object",
            "properties": {"texts": {"type": "array", "items": {"type": "string"}}},
            "required": ["texts"],
        },
        "output_schema": output_schema
        if output_schema is not None
        else {
            "type": "object",
            "properties": {
                "dense": {"type": "array"},
                "sparse": {"type": "array"},
            },
        },
        "defaults": defaults
        if defaults is not None
        else {
            "execution_timeout_ms": 60_000,
            "ttl_ms": 300_000,
            "result_delivery": "sync",
        },
        "hints_supported": hints_supported
        if hints_supported is not None
        else ["latency_sensitive", "cost_sensitive", "region_preference"],
    }
    if config_overrides:
        config.update(config_overrides)

    row: dict[str, Any] = {
        "id": id,
        "name": name,
        "version": version,
        "class": class_,
        "cost_mode": cost_mode,
        "config": config,
        "source": source,
        "last_applied_yaml_hash": last_applied_yaml_hash,
        "enabled": enabled,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    row.update(overrides)
    return row


def make_provider_row(
    *,
    id: str = "prov_bge_m3_lb_us_ks",
    capability_id: str = "cap_embedding_bge_m3",
    name: str = "bge-m3-lb-us-ks",
    provider_type: str = "serverless_lb",
    runpod_endpoint_id: str | None = "eptest00000000",
    runpod_template_id: str | None = None,
    region: str | None = "US-KS-2",
    cloud_type: str | None = None,
    config: dict[str, Any] | None = None,
    priority: int = 1,
    enabled: bool = True,
    health_status: str = "healthy",
    consecutive_failures: int = 0,
    cooldown_trips: int = 0,
    cold_start_p50_ms: int | None = 8_000,
    cold_start_p95_ms: int | None = 22_000,
    recent_error_rate: float = 0.0,
    cooldown_until: dt.datetime | None = None,
    source: str = "api",
    last_applied_yaml_hash: str | None = None,
    updated_at: dt.datetime = TEST_NOW,
    **overrides: Any,
) -> dict[str, Any]:
    """Build a dict-shaped asyncpg provider row for hermetic repository tests."""
    endpoint_id = runpod_endpoint_id or "eptest00000000"
    row: dict[str, Any] = {
        "id": id,
        "capability_id": capability_id,
        "name": name,
        "provider_type": provider_type,
        "runpod_endpoint_id": runpod_endpoint_id,
        "runpod_template_id": runpod_template_id,
        "region": region,
        "cloud_type": cloud_type,
        "config": config
        if config is not None
        else {
            "lb_base_url": f"https://{endpoint_id}.api.runpod.ai",
            "custom_paths": {"embed": "/embed", "health": "/ping"},
            "max_payload_mb": 30,
            "request_timeout_s": 330,
            "cost": {
                "mode": "per_second",
                "per_second_active": "0.000123",
            },
        },
        "priority": priority,
        "enabled": enabled,
        "health_status": health_status,
        "consecutive_failures": consecutive_failures,
        "cooldown_trips": cooldown_trips,
        "cold_start_p50_ms": cold_start_p50_ms,
        "cold_start_p95_ms": cold_start_p95_ms,
        "recent_error_rate": recent_error_rate,
        "cooldown_until": cooldown_until,
        "source": source,
        "last_applied_yaml_hash": last_applied_yaml_hash,
        "updated_at": updated_at,
    }
    row.update(overrides)
    return row


SEEDED_CAPABILITY_ROWS = (make_capability_row(),)
SEEDED_PROVIDER_ROWS = (make_provider_row(),)


def _copy_rows(rows: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [deepcopy(row) for row in rows]


def _async_mock(return_value: Any, side_effect: Any | None = None) -> AsyncMock:
    if side_effect is not None:
        return AsyncMock(side_effect=side_effect)
    return AsyncMock(return_value=return_value)


def make_asyncpg_pool(
    *,
    fetchrow: Any = None,
    fetch: list[Any] | None = None,
    fetchval: Any = None,
    execute: Any = "SELECT 1",
    fetchrow_side_effect: Any | None = None,
    fetch_side_effect: Any | None = None,
    fetchval_side_effect: Any | None = None,
    execute_side_effect: Any | None = None,
) -> MagicMock:
    """Return a MagicMock asyncpg pool with AsyncMock connection methods."""
    conn = MagicMock()
    conn.execute = _async_mock(execute, execute_side_effect)
    conn.fetchrow = _async_mock(fetchrow, fetchrow_side_effect)
    conn.fetch = _async_mock(fetch or [], fetch_side_effect)
    conn.fetchval = _async_mock(fetchval, fetchval_side_effect)

    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)

    acquire_context = MagicMock()
    acquire_context.__aenter__ = AsyncMock(return_value=conn)
    acquire_context.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_context)
    pool.conn = conn
    pool.acquire_context = acquire_context
    return pool


def make_asyncpg_budget_pool(
    *,
    current_spend: Decimal = Decimal("0"),
    admitted_id: str = "wkl_test",
    existing_id_for_key: str | None = None,
) -> MagicMock:
    def fetchval_side_effect(sql: str, *args: object) -> str | None:
        if "idempotency_key" in sql:
            return existing_id_for_key
        return admitted_id

    return make_asyncpg_pool(
        fetchrow={"s": current_spend},
        fetchval_side_effect=fetchval_side_effect,
    )


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _live_enabled(config: pytest.Config) -> bool:
    option_enabled = bool(config.getoption(RUN_LIVE_OPTION, default=False))
    env_enabled = any(_truthy(os.getenv(name)) for name in RUN_LIVE_ENV_VARS)
    return option_enabled or env_enabled


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        RUN_LIVE_OPTION,
        action="store_true",
        default=False,
        help="Run tests marked live against real external services.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live: calls real external services; skipped unless --run-live or RUNPOD_LIVE=1",
    )
    config.addinivalue_line(
        "markers",
        "asyncio: run an async test on AnyIO's asyncio backend",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not _live_enabled(config):
        skip_live = pytest.mark.skip(reason="live test: pass --run-live or set RUNPOD_LIVE=1")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)

    # Real-DB test modules connect to Postgres via DATABASE_URL with no skip
    # guard, so they must run only under the integration marker, never in the
    # default fast suite (`-m "not integration"`). Keeps the fast suite green
    # with no live DB even though conftest sets a placeholder DATABASE_URL for
    # import-time require_runtime_env during collection.
    for item in items:
        nodeid = item.nodeid.replace("\\", "/")
        if (
            any(nodeid.startswith(p) for p in _REAL_DB_TEST_MODULES)
            and "integration" not in item.keywords
        ):
            item.add_marker(pytest.mark.integration)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[None]:
    """Close pytest-asyncio's idle compatibility loop after fixture teardown.

    pytest-asyncio installs a fresh, unmarked loop after closing each test loop.
    A later synchronous CLI test that calls ``asyncio.run`` clears that current
    loop without closing it, so Python 3.13 reports its self-pipe socket during
    cyclic GC. No test relies on the deprecated implicit-current-loop behavior.
    """

    yield
    if "asyncio" not in item.keywords and "anyio" not in item.keywords:
        return
    policy = asyncio.get_event_loop_policy()
    try:
        # Python 3.12 warns when the compatibility loop is absent, while 3.13
        # raises RuntimeError. Both mean there is nothing to clean up.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="There is no current event loop",
                category=DeprecationWarning,
            )
            loop = policy.get_event_loop()
    except RuntimeError:
        return
    if not loop.is_running() and not loop.is_closed():
        loop.close()
    policy.set_event_loop(None)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Use one async backend so AnyIO tests do not also parametrize over Trio."""

    return "asyncio"


@pytest.fixture(scope="session")
def live_enabled(pytestconfig: pytest.Config) -> bool:
    return _live_enabled(pytestconfig)


@pytest.fixture(scope="session")
def test_now() -> dt.datetime:
    return TEST_NOW


@pytest.fixture
def capability_row_factory() -> RowFactory:
    return make_capability_row


@pytest.fixture
def provider_row_factory() -> RowFactory:
    return make_provider_row


@pytest.fixture
def seeded_capability_rows() -> list[dict[str, Any]]:
    return _copy_rows(SEEDED_CAPABILITY_ROWS)


@pytest.fixture
def seeded_provider_rows() -> list[dict[str, Any]]:
    return _copy_rows(SEEDED_PROVIDER_ROWS)


@pytest.fixture
def seeded_capability_row(seeded_capability_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return seeded_capability_rows[0]


@pytest.fixture
def seeded_provider_row(seeded_provider_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return seeded_provider_rows[0]


@pytest.fixture
def seeded_registry_rows() -> dict[str, list[dict[str, Any]]]:
    return {
        "capabilities": _copy_rows(SEEDED_CAPABILITY_ROWS),
        "providers": _copy_rows(SEEDED_PROVIDER_ROWS),
    }


@pytest.fixture
def asyncpg_pool_factory() -> AsyncpgPoolFactory:
    return make_asyncpg_pool


@pytest.fixture
def fake_asyncpg_pool_factory() -> AsyncpgPoolFactory:
    return make_asyncpg_pool


@pytest.fixture
def fake_asyncpg_pool() -> MagicMock:
    return make_asyncpg_pool()


@pytest.fixture
def asyncpg_pool(fake_asyncpg_pool: MagicMock) -> MagicMock:
    return fake_asyncpg_pool


@pytest.fixture
def budget_pool_factory() -> Callable[..., MagicMock]:
    return make_asyncpg_budget_pool


@pytest.fixture
def live_marker(live_enabled: bool) -> None:
    if not live_enabled:
        pytest.skip("live test: pass --run-live or set RUNPOD_LIVE=1")
    if not os.getenv("RUNPOD_API_KEY"):
        pytest.skip("live test: RUNPOD_API_KEY environment variable is required")


@pytest.fixture
def require_live_runpod(live_marker: None) -> None:
    return live_marker


@pytest.fixture
def respx_mock() -> Iterator[Any]:
    respx = pytest.importorskip(
        "respx",
        reason="respx is required for RunPod serverless HTTP mocks",
    )
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@dataclass
class MockTransportFactory:
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(
        self,
        handler: RequestHandler | None = None,
        *,
        status_code: int = 200,
        json: Any | None = None,
        text: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.MockTransport:
        def wrapped(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if handler is not None:
                return handler(request)
            return httpx.Response(
                status_code,
                json=json,
                text=text,
                headers=headers,
                request=request,
            )

        return httpx.MockTransport(wrapped)


@pytest.fixture
def mock_transport() -> MockTransportFactory:
    return MockTransportFactory()


@pytest.fixture
def httpx_mock_transport(mock_transport: MockTransportFactory) -> MockTransportFactory:
    return mock_transport


@pytest.fixture
def runpod_serverless_base_url() -> str:
    return "https://api.runpod.ai/v2/test-endpoint/openai/v1"


@pytest.fixture
def runpod_lb_endpoint_id() -> str:
    return "eptest00000000"


@pytest.fixture
def runpod_lb_base_url(runpod_lb_endpoint_id: str) -> str:
    return f"https://{runpod_lb_endpoint_id}.api.runpod.ai"


@pytest.fixture
def respx_runpod_serverless(
    respx_mock: Any,
    runpod_serverless_base_url: str,
) -> Callable[..., Any]:
    def register(
        path: str = "/chat/completions",
        *,
        status_code: int = 200,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return respx_mock.post(f"{runpod_serverless_base_url}{path}").mock(
            return_value=httpx.Response(status_code, json=json, headers=headers)
        )

    return register


@pytest.fixture
def respx_runpod_lb(
    respx_mock: Any,
    runpod_lb_base_url: str,
    runpod_response_factory: RunPodResponseFactory,
) -> Callable[..., Any]:
    def register(
        path: str = "/embed",
        *,
        method: str = "POST",
        response: httpx.Response | None = None,
        status_code: int = 200,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        route = respx_mock.request(method, f"{runpod_lb_base_url}{path}")
        return route.mock(
            return_value=response
            or (
                httpx.Response(status_code, json=json, headers=headers)
                if json is not None
                else runpod_response_factory.embedding_response(
                    status_code=status_code,
                    headers=headers,
                )
            )
        )

    return register


@pytest.fixture
def runpod_response_factory() -> RunPodResponseFactory:
    return RunPodResponseFactory()


@pytest.fixture
def runpod_rest_fake() -> RunPodRestFake:
    return RunPodRestFake()


@pytest.fixture
def fake_runpod_rest(runpod_rest_fake: RunPodRestFake) -> RunPodRestFake:
    return runpod_rest_fake


@pytest.fixture
def runpod_serverless_fake() -> RunPodServerlessFake:
    return RunPodServerlessFake()


@pytest.fixture
def runpod_lb_fake() -> RunPodLBFake:
    return RunPodLBFake()


@pytest.fixture
def fake_runpod_lb(runpod_lb_fake: RunPodLBFake) -> RunPodLBFake:
    return runpod_lb_fake


@pytest.fixture
def fake_runpod_serverless(
    runpod_serverless_fake: RunPodServerlessFake,
) -> RunPodServerlessFake:
    return runpod_serverless_fake


@pytest.fixture
def runpod_template_fake() -> RunPodTemplateFake:
    return RunPodTemplateFake()


@pytest.fixture
def fake_runpod_template(runpod_template_fake: RunPodTemplateFake) -> RunPodTemplateFake:
    return runpod_template_fake


@pytest.fixture
def fake_runpod_template_sdk() -> FakeRunPodTemplateSdk:
    return FakeRunPodTemplateSdk()


@pytest.fixture
def mock_runpod_endpoint(
    respx_mock: Any,
    runpod_response_factory: RunPodResponseFactory,
) -> Callable[..., Any]:
    def register(
        path: str = "/openai/v1/chat/completions",
        *,
        endpoint_id: str = "test-endpoint",
        method: str = "POST",
        response: httpx.Response | None = None,
        status_code: int = 200,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        route = respx_mock.request(
            method,
            f"https://api.runpod.ai/v2/{endpoint_id}{path}",
        )
        return route.mock(
            return_value=response
            or (
                httpx.Response(status_code, json=json, headers=headers)
                if json is not None
                else runpod_response_factory.chat_completion(
                    status_code=status_code,
                    headers=headers,
                )
            )
        )

    return register


@pytest.fixture
def fake_pod_state_machine() -> FakePodStateMachine:
    return FakePodStateMachine()


@pytest.fixture
def runpod_billing_fake() -> RunPodBillingFake:
    return RunPodBillingFake()


@pytest.fixture
def fake_runpod_billing(runpod_billing_fake: RunPodBillingFake) -> RunPodBillingFake:
    return runpod_billing_fake


class TrackingAsyncByteStream(httpx.AsyncByteStream):
    """SSE stream that tracks whether it was closed."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = tuple(chunks)
        self.closed = False

    async def __aiter__(self) -> Any:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class FailingAsyncByteStream(httpx.AsyncByteStream):
    """SSE stream that fails after a configurable number of chunks."""

    def __init__(self, chunks: list[bytes], fail_after: int) -> None:
        self._chunks = tuple(chunks)
        self._fail_after = fail_after
        self.closed = False

    async def __aiter__(self) -> Any:
        for i, chunk in enumerate(self._chunks):
            if i >= self._fail_after:
                raise httpx.ReadError("connection lost", request=MagicMock())
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def tracking_async_byte_stream() -> type[TrackingAsyncByteStream]:
    return TrackingAsyncByteStream


@pytest.fixture
def failing_async_byte_stream() -> type[FailingAsyncByteStream]:
    return FailingAsyncByteStream


_CHAT_COMPLETION_RESPONSE = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "qwen3-32b-awq",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    },
}


@pytest.fixture
def upstream_chat_completion_response(
    runpod_response_factory: RunPodResponseFactory,
) -> httpx.Response:
    return runpod_response_factory.chat_completion("OK")


@pytest.fixture
def upstream_chat_completion_json() -> dict[str, Any]:
    return _CHAT_COMPLETION_RESPONSE


def _env_for_app(**overrides: str) -> dict[str, str]:
    base: dict[str, str] = {
        "RUNPOD_API_KEY": "test-key",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "REDIS_URL": "redis://localhost:6379/0",
    }
    base.update(overrides)
    return base


@pytest.fixture
def app_env() -> dict[str, str]:
    return _env_for_app()


def make_llm_capability(
    *,
    capability_id: str = "cap_llm_qwen3_32b",
    name: str = "llm.qwen3-32b",
    version: str = "1.0.0",
    description: str = "Qwen3 32B AWQ",
    cost_mode: str = "per_second",
    enabled: bool = True,
    created_at: dt.datetime = TEST_NOW,
    updated_at: dt.datetime = TEST_NOW,
    **overrides: Any,
) -> Capability:
    return Capability(
        id=capability_id,
        name=name,
        version=version,
        class_=CapabilityClass.LLM,
        description=description,
        cost_mode=cost_mode,
        source=CapabilitySource.API,
        enabled=enabled,
        created_at=created_at,
        updated_at=updated_at,
    )


def make_provider(
    *,
    id: str = "prov_test",
    capability_id: str = "cap_llm_qwen3_32b",
    name: str = "test-provider",
    endpoint_id: str = "test-endpoint",
    provider_type: ProviderType = ProviderType.PUBLIC_ENDPOINT,
    priority: int = 1,
    fallback_chain: list[str] | None = None,
    enabled: bool = True,
    health_status: str = "healthy",
    openai_base_url: str | None = None,
    **config_overrides: Any,
) -> Provider:
    config: dict[str, Any] = {}
    if openai_base_url is not None:
        config["openai_base_url"] = openai_base_url
    else:
        config["openai_base_url"] = f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1"
    if fallback_chain is not None:
        config["fallback_chain"] = fallback_chain
    config.update(config_overrides)
    return Provider(
        id=id,
        capability_id=capability_id,
        name=name,
        provider_type=provider_type,
        runpod_endpoint_id=endpoint_id,
        config=config,
        priority=priority,
        enabled=enabled,
        health_status=health_status,
        updated_at=TEST_NOW,
    )


def make_provider_chain(
    *providers: Provider,
) -> list[Provider]:
    return list(providers)


@pytest.fixture
def llm_capability_factory() -> Callable[..., Capability]:
    return make_llm_capability


@pytest.fixture
def provider_factory() -> Callable[..., Provider]:
    return make_provider


@pytest.fixture
def provider_chain_factory() -> Callable[..., list[Provider]]:
    return make_provider_chain


@pytest.fixture
def clear_app_module():
    to_remove = [k for k in sys.modules if k.startswith("pitwall.api")]
    for k in to_remove:
        del sys.modules[k]
    yield
    to_remove = [k for k in sys.modules if k.startswith("pitwall.api")]
    for k in to_remove:
        del sys.modules[k]


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


def setup_openai_proxy_app(
    providers: list[Provider],
    capability: Capability | None = None,
    env: dict[str, str | None] | None = None,
):
    if capability is None:
        capability = make_llm_capability()

    mock_capability_repo = AsyncMock()
    mock_capability_repo.get_by_name.return_value = capability

    mock_provider_repo = AsyncMock()
    mock_provider_repo.list.return_value = providers

    app_env = _env_for_app(**{k: v for k, v in (env or {}).items() if v is not None})
    app_mod = _import_app(app_env)
    from pitwall.api.routes.openai import _capability_repo, _provider_repo

    app_mod.app.dependency_overrides[_capability_repo] = lambda: mock_capability_repo
    app_mod.app.dependency_overrides[_provider_repo] = lambda: mock_provider_repo

    mock_pool = MagicMock()
    app_mod.app.state.pool = mock_pool
    return app_mod, mock_capability_repo, mock_provider_repo


@pytest.fixture
def setup_openai_proxy_app_fixture(
    clear_app_module: None,
) -> Callable[..., tuple[Any, AsyncMock, AsyncMock]]:
    def _setup(
        providers: list[Provider],
        capability: Capability | None = None,
        env: dict[str, str | None] | None = None,
    ):
        return setup_openai_proxy_app(providers, capability, env)

    return _setup


async def _chat_post(
    app_mod: Any,
    path: str = "/v1/openai/llm.qwen3-32b/v1/chat/completions",
    json: dict[str, Any | None] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    if json is None:
        json = {
            "model": "qwen3-32b-awq",
            "messages": [{"role": "user", "content": "hello"}],
        }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_mod.app),
        base_url="http://test",
    ) as client:
        return await client.post(path, json=json, **kwargs)


@pytest.fixture
def openai_proxy_chat_post(
    clear_app_module: None,
) -> Callable[..., Any]:
    async def _poster(
        app_mod: Any,
        path: str = "/v1/openai/llm.qwen3-32b/v1/chat/completions",
        json: dict[str, Any | None] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        return await _chat_post(app_mod, path, json, **kwargs)

    return _poster


@pytest.fixture
def fake_mcp_recorder() -> FakeServiceLayerRecorder:
    """Return a FakeServiceLayerRecorder for hermetic MCP tool testing.

    Records MCP tool invocations without hitting live services.
    Install before use and uninstall after to avoid polluting global state.
    """
    from tests.fakes.mcp import FakeServiceLayerRecorder

    recorder = FakeServiceLayerRecorder()
    recorder.install()
    try:
        yield recorder
    finally:
        recorder.uninstall()
        recorder.reset()
