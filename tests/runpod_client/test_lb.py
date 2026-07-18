"""Tests for LBClient — RunPod load-balancing Serverless helpers."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
import respx

from pitwall.runpod_client.lb import LBClient, LBResponse, lb_endpoint_url
from tests.fakes.runpod import RunPodLBFake as LBFake

pytestmark = pytest.mark.anyio

ENDPOINT_ID = "eptest00000000"


def _lb_url(endpoint_id: str = ENDPOINT_ID, path: str = "/") -> str:
    return f"https://{endpoint_id}.api.runpod.ai{path}"


def test_lb_endpoint_url_with_custom_path() -> None:
    assert lb_endpoint_url("eptest00000000", "/embed") == _lb_url(path="/embed")


def test_lb_endpoint_url_default_path() -> None:
    assert lb_endpoint_url("eptest00000000") == _lb_url(path="/")


def test_lb_endpoint_url_strips_trailing_slash_from_id() -> None:
    assert lb_endpoint_url("abc/") == "https://abc.api.runpod.ai/"


def test_lb_endpoint_url_prepends_slash_to_path() -> None:
    assert lb_endpoint_url("abc", "embed") == "https://abc.api.runpod.ai/embed"


@respx.mock
async def test_post_sends_json_and_returns_lb_response() -> None:
    payload: dict[str, Any] = {"embedding": [0.1, 0.2, 0.3]}
    route = respx.post(_lb_url(path="/embed"))
    route.mock(return_value=httpx.Response(200, json=payload))

    client = LBClient(api_key="test-key")
    result = await client.post(ENDPOINT_ID, "/embed", json={"text": "hello"})

    assert isinstance(result, LBResponse)
    assert result.status_code == 200
    assert result.data == payload
    assert route.call_count == 1


@respx.mock
async def test_post_sends_auth_header() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True})

    respx.post(_lb_url(path="/embed")).mock(side_effect=handler)

    client = LBClient(api_key="secret-key")
    await client.post(ENDPOINT_ID, "/embed", json={"text": "x"})

    assert captured["auth"] == "Bearer secret-key"


@respx.mock
async def test_get_returns_lb_response() -> None:
    payload: dict[str, Any] = {"status": "healthy", "workers": 3}
    route = respx.get(_lb_url(path="/info"))
    route.mock(return_value=httpx.Response(200, json=payload))

    client = LBClient(api_key="test-key")
    result = await client.get(ENDPOINT_ID, "/info")

    assert result.status_code == 200
    assert result.data == payload
    assert route.call_count == 1


@respx.mock
async def test_ping_returns_true_on_200() -> None:
    respx.get(_lb_url(path="/ping")).mock(return_value=httpx.Response(200, text="ok"))

    client = LBClient(api_key="test-key")
    result = await client.ping(ENDPOINT_ID)

    assert result is True


@respx.mock
async def test_ping_returns_false_on_non_200() -> None:
    respx.get(_lb_url(path="/ping")).mock(return_value=httpx.Response(503, text="unavailable"))

    client = LBClient(api_key="test-key")
    result = await client.ping(ENDPOINT_ID)

    assert result is False


@respx.mock
async def test_post_raises_on_server_error() -> None:
    respx.post(_lb_url(path="/embed")).mock(
        return_value=httpx.Response(500, json={"error": "internal"})
    )

    client = LBClient(api_key="test-key")
    with pytest.raises(httpx.HTTPStatusError):
        await client.post(ENDPOINT_ID, "/embed", json={"text": "x"})


@respx.mock
async def test_get_raises_on_not_found() -> None:
    respx.get(_lb_url(path="/missing")).mock(return_value=httpx.Response(404, text="not found"))

    client = LBClient(api_key="test-key")
    with pytest.raises(httpx.HTTPStatusError):
        await client.get(ENDPOINT_ID, "/missing")


@respx.mock
async def test_post_handles_non_json_response() -> None:
    route = respx.post(_lb_url(path="/raw"))
    route.mock(return_value=httpx.Response(200, text="plain text"))

    client = LBClient(api_key="test-key")
    result = await client.post(ENDPOINT_ID, "/raw")

    assert result.status_code == 200
    assert result.data == {}


@respx.mock
async def test_post_handles_json_array_response() -> None:
    route = respx.post(_lb_url(path="/list"))
    route.mock(return_value=httpx.Response(200, json=[1, 2, 3]))

    client = LBClient(api_key="test-key")
    result = await client.post(ENDPOINT_ID, "/list")

    assert result.status_code == 200
    assert result.data == {"value": [1, 2, 3]}


# --- httpx.MockTransport tests (LBFake) -----------------------------------


async def _completed_sleep(_: float) -> None:
    return None


async def test_lb_retries_429_after_numeric_seconds() -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    fake = LBFake()
    fake.add_response(httpx.Response(429, headers={"Retry-After": "30"}))
    fake.add_response(httpx.Response(200, json={"embedding": [0.1, 0.2]}))

    client = LBClient(
        api_key="test-key",
        retry_delays=(0.0,),
        max_retry_after_s=5.0,
        sleep=capture_sleep,
        transport=fake.transport(),
    )

    result = await client.post(ENDPOINT_ID, "/embed", json={"text": "hello"})

    assert result.status_code == 200
    assert result.data == {"embedding": [0.1, 0.2]}
    assert sleeps == [5.0]
    assert len(fake.requests) == 2


async def test_lb_retries_429_after_http_date() -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    fake = LBFake()
    fake.add_response(httpx.Response(429, headers={"Retry-After": "Thu, 28 May 2026 12:00:03 GMT"}))
    fake.add_response(httpx.Response(200, json={"embedding": [0.3, 0.4]}))

    client = LBClient(
        api_key="test-key",
        retry_delays=(0.0,),
        max_retry_after_s=10.0,
        sleep=capture_sleep,
        clock=lambda: dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC),
        transport=fake.transport(),
    )

    result = await client.post(ENDPOINT_ID, "/embed", json={"text": "world"})

    assert result.status_code == 200
    assert sleeps == [3.0]
    assert len(fake.requests) == 2


async def test_lb_retries_5xx_then_succeeds() -> None:
    fake = LBFake()
    fake.add_response(httpx.Response(503))
    fake.add_response(httpx.Response(200, json={"status": "ok"}))

    client = LBClient(
        api_key="test-key",
        retry_delays=(0.0, 0.0),
        sleep=_completed_sleep,
        transport=fake.transport(),
    )

    result = await client.post(ENDPOINT_ID, "/embed", json={"text": "test"})

    assert result.status_code == 200
    assert len(fake.requests) == 2


async def test_lb_raises_on_non_rate_limit_4xx() -> None:
    fake = LBFake()
    fake.add_response(httpx.Response(401, json={"error": "unauthorized"}))

    client = LBClient(
        api_key="bad-key",
        sleep=_completed_sleep,
        transport=fake.transport(),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.post(ENDPOINT_ID, "/embed", json={"text": "x"})


async def test_lb_exhausts_retries_on_persistent_429() -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    fake = LBFake()
    fake.add_response(httpx.Response(429, headers={"Retry-After": "30"}))
    fake.add_response(httpx.Response(429, headers={"Retry-After": "30"}))
    fake.add_response(httpx.Response(429, headers={"Retry-After": "30"}))

    client = LBClient(
        api_key="test-key",
        retry_delays=(0.0, 0.0),
        max_retry_after_s=5.0,
        sleep=capture_sleep,
        transport=fake.transport(),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.post(ENDPOINT_ID, "/embed", json={"text": "x"})

    assert sleeps == [5.0, 5.0]
    assert len(fake.requests) == 3


# --- respx.mock 429 tests --------------------------------------------------


@respx.mock
async def test_respx_lb_retries_429_after_numeric() -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    route = respx.post(_lb_url(path="/embed"))
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "30"}),
        httpx.Response(200, json={"embedding": [0.1, 0.2]}),
    ]

    client = LBClient(
        api_key="test-key",
        retry_delays=(0.0,),
        max_retry_after_s=5.0,
        sleep=capture_sleep,
    )

    result = await client.post(ENDPOINT_ID, "/embed", json={"text": "hello"})

    assert result.status_code == 200
    assert sleeps == [5.0]
    assert route.call_count == 2


@respx.mock
async def test_respx_lb_retries_429_after_http_date() -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    route = respx.post(_lb_url(path="/embed"))
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "Thu, 28 May 2026 12:00:03 GMT"}),
        httpx.Response(200, json={"embedding": [0.3, 0.4]}),
    ]

    client = LBClient(
        api_key="test-key",
        retry_delays=(0.0,),
        max_retry_after_s=10.0,
        sleep=capture_sleep,
        clock=lambda: dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC),
    )

    result = await client.post(ENDPOINT_ID, "/embed", json={"text": "world"})

    assert result.status_code == 200
    assert sleeps == [3.0]
    assert route.call_count == 2


@respx.mock
async def test_respx_lb_retries_5xx_then_succeeds() -> None:
    route = respx.post(_lb_url(path="/embed"))
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json={"status": "ok"}),
    ]

    client = LBClient(
        api_key="test-key",
        retry_delays=(0.0, 0.0),
        sleep=_completed_sleep,
    )

    result = await client.post(ENDPOINT_ID, "/embed", json={"text": "test"})

    assert result.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_respx_lb_raises_on_non_rate_limit_4xx() -> None:
    respx.post(_lb_url(path="/embed")).mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )

    client = LBClient(
        api_key="bad-key",
        sleep=_completed_sleep,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.post(ENDPOINT_ID, "/embed", json={"text": "x"})


@respx.mock
async def test_respx_lb_exhausts_retries_on_persistent_5xx() -> None:
    route = respx.post(_lb_url(path="/embed"))
    route.mock(return_value=httpx.Response(500, json={"error": "internal"}))

    client = LBClient(
        api_key="test-key",
        retry_delays=(0.0, 0.0),
        sleep=_completed_sleep,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.post(ENDPOINT_ID, "/embed", json={"text": "x"})

    assert route.call_count == 3
