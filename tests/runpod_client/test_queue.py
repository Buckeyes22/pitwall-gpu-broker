"""Tests for QueueClient — RunPod queue-based Serverless helpers."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
import respx

from pitwall.runpod_client.queue import (
    RUNPOD_API_BASE,
    QueueCancelResult,
    QueueClient,
    QueueHealth,
    QueueJob,
    QueuePurgeResult,
    RateLimitFailure,
    queue_url,
)

pytestmark = pytest.mark.anyio

ENDPOINT_ID = "abc123"


def _queue_base(endpoint_id: str = ENDPOINT_ID) -> str:
    return f"{RUNPOD_API_BASE}/{endpoint_id}"


def test_queue_url_with_path() -> None:
    assert queue_url("abc123", "/runsync") == f"{_queue_base()}/runsync"


def test_queue_url_without_path() -> None:
    assert queue_url("abc123") == _queue_base()


def test_queue_url_bare() -> None:
    assert queue_url("abc123") == "https://api.runpod.ai/v2/abc123"


@respx.mock
async def test_runsync_posts_and_returns_queue_job() -> None:
    payload: dict[str, Any] = {
        "id": "job-1",
        "status": "COMPLETED",
        "output": {"result": 42},
    }
    route = respx.post(f"{_queue_base()}/runsync")
    route.mock(return_value=httpx.Response(200, json=payload))

    client = QueueClient(api_key="test-key")
    result = await client.runsync(ENDPOINT_ID, input={"prompt": "hello"})

    assert isinstance(result, QueueJob)
    assert result.id == "job-1"
    assert result.status == "COMPLETED"
    assert result.output == {"result": 42}
    assert result.raw == payload
    assert route.call_count == 1


@respx.mock
async def test_runsync_sends_auth_header() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"id": "j1", "status": "COMPLETED"})

    respx.post(f"{_queue_base()}/runsync").mock(side_effect=handler)

    client = QueueClient(api_key="secret-xyz")
    await client.runsync(ENDPOINT_ID, input={"x": 1})

    assert captured["auth"] == "Bearer secret-xyz"


@respx.mock
async def test_runsync_sends_webhook_and_policy() -> None:
    import json

    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(200, json={"id": "j1", "status": "COMPLETED"})

    respx.post(f"{_queue_base()}/runsync").mock(side_effect=handler)

    client = QueueClient(api_key="k")
    await client.runsync(
        ENDPOINT_ID,
        input={"a": 1},
        webhook="https://example.com/hook",
        policy={"executionTimeout": 60000},
    )

    body = json.loads(captured["body"])
    assert body["webhook"] == "https://example.com/hook"
    assert body["policy"]["executionTimeout"] == 60000


@respx.mock
async def test_run_posts_and_returns_queue_job() -> None:
    payload: dict[str, Any] = {
        "id": "job-2",
        "status": "IN_QUEUE",
    }
    route = respx.post(f"{_queue_base()}/run")
    route.mock(return_value=httpx.Response(200, json=payload))

    client = QueueClient(api_key="test-key")
    result = await client.run(ENDPOINT_ID, input={"prompt": "go"})

    assert result.id == "job-2"
    assert result.status == "IN_QUEUE"
    assert result.output is None
    assert route.call_count == 1


@respx.mock
async def test_status_gets_and_returns_queue_job() -> None:
    payload: dict[str, Any] = {
        "id": "job-3",
        "status": "IN_PROGRESS",
        "output": None,
    }
    route = respx.get(f"{_queue_base()}/status/job-3")
    route.mock(return_value=httpx.Response(200, json=payload))

    client = QueueClient(api_key="test-key")
    result = await client.status(ENDPOINT_ID, "job-3")

    assert result.id == "job-3"
    assert result.status == "IN_PROGRESS"
    assert route.call_count == 1


@respx.mock
async def test_health_returns_queue_health() -> None:
    payload: dict[str, Any] = {
        "queued": 5,
        "running": 2,
        "completed": 100,
    }
    route = respx.get(f"{_queue_base()}/health")
    route.mock(return_value=httpx.Response(200, json=payload))

    client = QueueClient(api_key="test-key")
    result = await client.health(ENDPOINT_ID)

    assert isinstance(result, QueueHealth)
    assert result.jobs == {"queued": 5, "running": 2, "completed": 100}
    assert result.raw == payload
    assert route.call_count == 1


@respx.mock
async def test_cancel_posts_and_returns_typed_result() -> None:
    payload: dict[str, Any] = {"cancelled": True}
    route = respx.post(f"{_queue_base()}/cancel/job-4")
    route.mock(return_value=httpx.Response(200, json=payload))

    client = QueueClient(api_key="test-key")
    result = await client.cancel(ENDPOINT_ID, "job-4")

    assert isinstance(result, QueueCancelResult)
    assert result.cancelled is True
    assert result.raw == payload
    assert route.call_count == 1


@respx.mock
async def test_purge_queue_posts_and_returns_typed_result() -> None:
    payload: dict[str, Any] = {"purged": 7}
    route = respx.post(f"{_queue_base()}/purge-queue")
    route.mock(return_value=httpx.Response(200, json=payload))

    client = QueueClient(api_key="test-key")
    result = await client.purge_queue(ENDPOINT_ID)

    assert isinstance(result, QueuePurgeResult)
    assert result.purged == 7
    assert result.raw == payload
    assert route.call_count == 1


@respx.mock
async def test_runsync_raises_on_server_error() -> None:
    respx.post(f"{_queue_base()}/runsync").mock(
        return_value=httpx.Response(500, json={"error": "internal"})
    )

    client = QueueClient(api_key="test-key")
    with pytest.raises(httpx.HTTPStatusError):
        await client.runsync(ENDPOINT_ID, input={"x": 1})


@respx.mock
async def test_status_raises_on_not_found() -> None:
    respx.get(f"{_queue_base()}/status/no-such-job").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )

    client = QueueClient(api_key="test-key")
    with pytest.raises(httpx.HTTPStatusError):
        await client.status(ENDPOINT_ID, "no-such-job")


def test_queue_job_parses_error_field() -> None:
    job = QueueJob(
        id="j1",
        status="FAILED",
        output=None,
        error="OOM",
        raw={"id": "j1", "status": "FAILED", "error": "OOM"},
    )
    assert job.error == "OOM"
    assert job.status == "FAILED"


async def _noop_sleep(_: float) -> None:
    return None


# --- 429 / Retry-After tests ------------------------------------------------


@respx.mock
async def test_runsync_retries_429_after_numeric_seconds() -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    route = respx.post(f"{_queue_base()}/runsync")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "30"}),
        httpx.Response(200, json={"id": "j1", "status": "COMPLETED"}),
    ]

    client = QueueClient(
        api_key="test-key",
        retry_delays=(0.0,),
        max_retry_after_s=5.0,
        sleep=capture_sleep,
    )
    result = await client.runsync(ENDPOINT_ID, input={"prompt": "hello"})

    assert result.id == "j1"
    assert result.status == "COMPLETED"
    assert sleeps == [5.0]
    assert route.call_count == 2


@respx.mock
async def test_runsync_retries_429_after_http_date() -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    route = respx.post(f"{_queue_base()}/runsync")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "Thu, 28 May 2026 12:00:03 GMT"}),
        httpx.Response(200, json={"id": "j2", "status": "COMPLETED"}),
    ]

    client = QueueClient(
        api_key="test-key",
        retry_delays=(0.0,),
        max_retry_after_s=10.0,
        sleep=capture_sleep,
        clock=lambda: dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC),
    )
    result = await client.runsync(ENDPOINT_ID, input={"prompt": "hello"})

    assert result.id == "j2"
    assert sleeps == [3.0]
    assert route.call_count == 2


@respx.mock
async def test_run_retries_429_and_succeeds() -> None:
    route = respx.post(f"{_queue_base()}/run")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"id": "j3", "status": "IN_QUEUE"}),
    ]

    client = QueueClient(
        api_key="test-key",
        retry_delays=(0.0,),
        sleep=_noop_sleep,
    )
    result = await client.run(ENDPOINT_ID, input={"x": 1})

    assert result.id == "j3"
    assert result.status == "IN_QUEUE"
    assert route.call_count == 2


@respx.mock
async def test_runsync_raises_on_non_rate_limit_4xx() -> None:
    respx.post(f"{_queue_base()}/runsync").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )

    client = QueueClient(api_key="bad-key", sleep=_noop_sleep)
    with pytest.raises(httpx.HTTPStatusError):
        await client.runsync(ENDPOINT_ID, input={"x": 1})


@respx.mock
async def test_runsync_exhausts_retries_on_persistent_429() -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    route = respx.post(f"{_queue_base()}/runsync")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "30"}),
        httpx.Response(429, headers={"Retry-After": "30"}),
        httpx.Response(429, headers={"Retry-After": "30"}),
    ]

    client = QueueClient(
        api_key="test-key",
        retry_delays=(0.0, 0.0),
        max_retry_after_s=5.0,
        sleep=capture_sleep,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.runsync(ENDPOINT_ID, input={"x": 1})

    assert sleeps == [5.0, 5.0]
    assert route.call_count == 3


@respx.mock
async def test_on_429_callback_receives_endpoint_context() -> None:
    failures: list[RateLimitFailure] = []

    async def record_failure(failure: RateLimitFailure) -> None:
        failures.append(failure)

    route = respx.post(f"{_queue_base()}/runsync")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "10"}),
        httpx.Response(200, json={"id": "j4", "status": "COMPLETED"}),
    ]

    now = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)
    client = QueueClient(
        api_key="test-key",
        retry_delays=(0.0,),
        max_retry_after_s=10.0,
        sleep=_noop_sleep,
        clock=lambda: now,
        on_429=record_failure,
    )
    result = await client.runsync(ENDPOINT_ID, input={"x": 1})

    assert result.id == "j4"
    assert len(failures) == 1
    failure = failures[0]
    assert failure.endpoint_id == ENDPOINT_ID
    assert failure.path == "/runsync"
    assert failure.retry_after_s == 10.0
    assert failure.retry_after_header == "10"
    assert failure.status_code == 429
    assert failure.occurred_at == now


@respx.mock
async def test_on_429_callback_called_on_every_429_attempt() -> None:
    failures: list[RateLimitFailure] = []

    async def record_failure(failure: RateLimitFailure) -> None:
        failures.append(failure)

    route = respx.post(f"{_queue_base()}/runsync")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "5"}),
        httpx.Response(429, headers={"Retry-After": "5"}),
        httpx.Response(200, json={"id": "j5", "status": "COMPLETED"}),
    ]

    client = QueueClient(
        api_key="test-key",
        retry_delays=(0.0, 0.0),
        max_retry_after_s=5.0,
        sleep=_noop_sleep,
        on_429=record_failure,
    )
    result = await client.runsync(ENDPOINT_ID, input={"x": 1})

    assert result.id == "j5"
    assert len(failures) == 2
    assert all(f.endpoint_id == ENDPOINT_ID for f in failures)
    assert all(f.path == "/runsync" for f in failures)
    assert all(f.status_code == 429 for f in failures)


@respx.mock
async def test_on_429_not_called_when_no_429() -> None:
    failures: list[RateLimitFailure] = []

    async def record_failure(failure: RateLimitFailure) -> None:
        failures.append(failure)

    respx.post(f"{_queue_base()}/runsync").mock(
        return_value=httpx.Response(200, json={"id": "j6", "status": "COMPLETED"})
    )

    client = QueueClient(
        api_key="test-key",
        sleep=_noop_sleep,
        on_429=record_failure,
    )
    await client.runsync(ENDPOINT_ID, input={"x": 1})

    assert failures == []


@respx.mock
async def test_runsync_429_uses_configured_backoff_when_no_retry_after() -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    route = respx.post(f"{_queue_base()}/runsync")
    route.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json={"id": "j7", "status": "COMPLETED"}),
    ]

    client = QueueClient(
        api_key="test-key",
        retry_delays=(2.5,),
        sleep=capture_sleep,
    )
    result = await client.runsync(ENDPOINT_ID, input={"x": 1})

    assert result.id == "j7"
    assert sleeps == [2.5]
    assert route.call_count == 2
