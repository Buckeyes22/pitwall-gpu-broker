from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest
import respx

from pitwall.runpod_client.serverless import (
    ServerlessClient,
    ServerlessResponse,
    parse_retry_after,
)
from tests.fakes.runpod import RunPodResponseFactory, RunPodServerlessFake

pytestmark = pytest.mark.anyio

BASE_URL = "https://api.runpod.ai/v2/test-endpoint/openai/v1"


async def _completed_sleep(_: float) -> None:
    return None


def test_parse_retry_after_numeric_seconds_are_bounded() -> None:
    assert parse_retry_after("2", max_delay_s=60.0) == 2.0
    assert parse_retry_after("120", max_delay_s=10.0) == 10.0
    assert parse_retry_after("-4", max_delay_s=10.0) == 0.0


def test_parse_retry_after_http_date_uses_utc_delta() -> None:
    now = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)

    assert parse_retry_after("Thu, 28 May 2026 12:00:03 GMT", now=now, max_delay_s=10.0) == 3.0
    assert parse_retry_after("Thu, 28 May 2026 12:00:30 GMT", now=now, max_delay_s=10.0) == 10.0
    assert parse_retry_after("Thu, 28 May 2026 11:59:59 GMT", now=now, max_delay_s=10.0) == 0.0


def test_parse_retry_after_invalid_values_return_none() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("not a date") is None
    assert parse_retry_after("nan") is None


async def test_chat_completion_returns_parsed_response(
    runpod_serverless_fake: RunPodServerlessFake,
) -> None:
    runpod_serverless_fake.add_chat_completion(
        "OK",
        model="qwen3-6-27b",
        prompt_tokens=5,
        completion_tokens=1,
    )
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        timeout_s=10,
        sleep=_completed_sleep,
        transport=runpod_serverless_fake.transport(),
    )

    result = await client.chat_completion(
        messages=[{"role": "user", "content": "Say OK"}],
        max_tokens=8,
        temperature=0.0,
    )
    await client.aclose()

    assert isinstance(result, ServerlessResponse)
    assert result.content == "OK"
    assert result.model == "qwen3-6-27b"
    assert result.input_tokens == 5
    assert result.output_tokens == 1
    assert result.finish_reason == "stop"
    assert result.duration_ms > 0


async def test_chat_completion_sends_authorization_header(
    runpod_serverless_fake: RunPodServerlessFake,
) -> None:
    runpod_serverless_fake.add_chat_completion(model="qwen3-6-27b")
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="secret-abc",
        model="qwen3-6-27b",
        sleep=_completed_sleep,
        transport=runpod_serverless_fake.transport(),
    )

    await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    request = runpod_serverless_fake.requests[0]
    body: dict[str, Any] = json.loads(request.content)
    assert request.headers.get("authorization") == "Bearer secret-abc"
    assert body["model"] == "qwen3-6-27b"
    assert body["max_tokens"] == 4


async def test_chat_completion_retries_on_5xx_then_succeeds(
    runpod_serverless_fake: RunPodServerlessFake,
) -> None:
    runpod_serverless_fake.add_response(httpx.Response(503))
    runpod_serverless_fake.add_chat_completion()
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        retry_delays=(0.0, 0.0, 0.0),
        sleep=_completed_sleep,
        transport=runpod_serverless_fake.transport(),
    )

    result = await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    assert result.content == "ok"
    assert len(runpod_serverless_fake.requests) == 2


async def test_chat_completion_retries_429_after_numeric_seconds(
    runpod_serverless_fake: RunPodServerlessFake,
) -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    runpod_serverless_fake.add_response(httpx.Response(429, headers={"Retry-After": "30"}))
    runpod_serverless_fake.add_chat_completion()
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        retry_delays=(0.0,),
        max_retry_after_s=5.0,
        sleep=capture_sleep,
        transport=runpod_serverless_fake.transport(),
    )

    result = await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    assert result.content == "ok"
    assert sleeps == [5.0]
    assert len(runpod_serverless_fake.requests) == 2


async def test_chat_completion_retries_429_after_http_date(
    runpod_serverless_fake: RunPodServerlessFake,
) -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    runpod_serverless_fake.add_response(
        httpx.Response(429, headers={"Retry-After": "Thu, 28 May 2026 12:00:03 GMT"})
    )
    runpod_serverless_fake.add_chat_completion()
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        retry_delays=(0.0,),
        max_retry_after_s=10.0,
        sleep=capture_sleep,
        clock=lambda: dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC),
        transport=runpod_serverless_fake.transport(),
    )

    result = await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    assert result.content == "ok"
    assert sleeps == [3.0]
    assert len(runpod_serverless_fake.requests) == 2


async def test_chat_completion_raises_on_non_rate_limit_4xx(
    runpod_serverless_fake: RunPodServerlessFake,
) -> None:
    runpod_serverless_fake.add_response(httpx.Response(401, json={"error": "unauthorized"}))
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="bad-key",
        model="qwen3-6-27b",
        sleep=_completed_sleep,
        transport=runpod_serverless_fake.transport(),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()


# --- respx.mock tests -------------------------------------------------------


def test_base_url_must_end_with_openai_v1() -> None:
    with pytest.raises(ValueError, match="base_url must end with /openai/v1"):
        ServerlessClient(
            base_url="https://api.runpod.ai/v2/test-endpoint",
            api_key="test-key",
            model="qwen3-6-27b",
        )


@respx.mock
async def test_respx_5xx_retry_then_succeeds(
    runpod_response_factory: RunPodResponseFactory,
) -> None:
    route = respx.post(f"{BASE_URL}/chat/completions")
    route.side_effect = [
        httpx.Response(503),
        runpod_response_factory.chat_completion("ok"),
    ]
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        retry_delays=(0.0, 0.0, 0.0),
        sleep=_completed_sleep,
    )
    result = await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    assert result.content == "ok"
    assert route.call_count == 2


@respx.mock
async def test_respx_429_retry_after_numeric(
    runpod_response_factory: RunPodResponseFactory,
) -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    route = respx.post(f"{BASE_URL}/chat/completions")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "30"}),
        runpod_response_factory.chat_completion("ok"),
    ]
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        retry_delays=(0.0,),
        max_retry_after_s=5.0,
        sleep=capture_sleep,
    )
    result = await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    assert result.content == "ok"
    assert sleeps == [5.0]
    assert route.call_count == 2


@respx.mock
async def test_respx_auth_header_sent(
    runpod_response_factory: RunPodResponseFactory,
) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return runpod_response_factory.chat_completion(request=request)

    respx.post(f"{BASE_URL}/chat/completions").mock(side_effect=handler)
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="secret-abc",
        model="qwen3-6-27b",
        sleep=_completed_sleep,
    )
    await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    assert captured["auth"] == "Bearer secret-abc"


@respx.mock
async def test_respx_request_url_is_normalized(
    runpod_response_factory: RunPodResponseFactory,
) -> None:
    route = respx.post(f"{BASE_URL}/chat/completions")
    route.mock(return_value=runpod_response_factory.chat_completion())
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        sleep=_completed_sleep,
    )
    await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    assert route.call_count == 1
    assert str(route.calls[0].request.url) == f"{BASE_URL}/chat/completions"


@respx.mock
async def test_respx_429_retry_after_http_date(
    runpod_response_factory: RunPodResponseFactory,
) -> None:
    sleeps: list[float] = []

    async def capture_sleep(delay_s: float) -> None:
        sleeps.append(delay_s)

    route = respx.post(f"{BASE_URL}/chat/completions")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "Thu, 28 May 2026 12:00:03 GMT"}),
        runpod_response_factory.chat_completion("ok"),
    ]
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        retry_delays=(0.0,),
        max_retry_after_s=10.0,
        sleep=capture_sleep,
        clock=lambda: dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC),
    )
    result = await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    assert result.content == "ok"
    assert sleeps == [3.0]
    assert route.call_count == 2


@respx.mock
async def test_respx_non_rate_limit_4xx_raises() -> None:
    respx.post(f"{BASE_URL}/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="bad-key",
        model="qwen3-6-27b",
        sleep=_completed_sleep,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()


@respx.mock
async def test_respx_exhausts_retries_on_persistent_5xx() -> None:
    route = respx.post(f"{BASE_URL}/chat/completions")
    route.mock(return_value=httpx.Response(500, json={"error": "internal"}))
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        retry_delays=(0.0, 0.0),
        sleep=_completed_sleep,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()
    assert route.call_count == 3


@respx.mock
async def test_respx_chat_completion_returns_parsed_response() -> None:
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        timeout_s=10,
        sleep=_completed_sleep,
    )
    async with respx.mock:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "model": "qwen3-6-27b",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 1,
                        "total_tokens": 6,
                    },
                },
            )
        )
        result = await client.chat_completion(
            messages=[{"role": "user", "content": "Say OK"}],
            max_tokens=8,
            temperature=0.0,
        )
    await client.aclose()
    assert isinstance(result, ServerlessResponse)
    assert result.content == "OK"
    assert result.model == "qwen3-6-27b"
    assert result.input_tokens == 5
    assert result.output_tokens == 1
    assert result.finish_reason == "stop"
    assert result.duration_ms > 0


@respx.mock
async def test_respx_chat_completion_sends_authorization_header_and_body() -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "qwen3-6-27b",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="secret-abc",
        model="qwen3-6-27b",
        sleep=_completed_sleep,
    )
    async with respx.mock:
        respx.post(f"{BASE_URL}/chat/completions").mock(side_effect=_handler)
        await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()
    assert captured["auth"] == "Bearer secret-abc"
    assert captured["body"]["model"] == "qwen3-6-27b"
    assert captured["body"]["max_tokens"] == 4


@respx.mock
async def test_respx_chat_completion_retries_on_5xx_then_succeeds() -> None:
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        retry_delays=(0.0, 0.0, 0.0),
        sleep=_completed_sleep,
    )
    async with respx.mock:
        route = respx.post(f"{BASE_URL}/chat/completions")
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(
                200,
                json={
                    "model": "qwen3-6-27b",
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            ),
        ]
        result = await client.chat_completion(
            messages=[{"role": "user", "content": "x"}], max_tokens=4
        )
    await client.aclose()
    assert result.content == "ok"
    assert route.call_count == 2


@respx.mock
async def test_respx_chat_completion_raises_on_4xx() -> None:
    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="bad-key",
        model="qwen3-6-27b",
        sleep=_completed_sleep,
    )
    async with respx.mock:
        respx.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()


# --- httpx.MockTransport timing tests -----------------------------------------


async def test_mock_transport_5xx_retries_then_succeeds() -> None:
    """Fast 5xx: MockTransport returns 503 then 200; client retries and succeeds."""

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503, json={"error": "try again"})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "qwen3-6-27b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        retry_delays=(0.0, 0.0, 0.0),
        sleep=_completed_sleep,
        transport=httpx.MockTransport(handler),
    )

    result = await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    assert result.content == "ok"
    assert call_count == 2


async def test_mock_transport_transport_error_retries_then_succeeds() -> None:
    """Transport errors are retried and then succeed on subsequent attempt."""

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "qwen3-6-27b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        retry_delays=(0.0, 0.0, 0.0),
        sleep=_completed_sleep,
        transport=httpx.MockTransport(handler),
    )

    result = await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    assert result.content == "ok"
    assert call_count == 2


async def test_mock_transport_transport_error_exhausts_retries() -> None:
    """Transport errors exhaust all retries and raise the last exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        retry_delays=(0.0, 0.0),
        sleep=_completed_sleep,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.ConnectError):
        await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()


async def test_mock_transport_4xx_no_retry() -> None:
    """4xx errors are not retried - no fallback behavior."""

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="bad-key",
        model="qwen3-6-27b",
        retry_delays=(0.0, 0.0, 0.0),
        sleep=_completed_sleep,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    assert call_count == 1


async def test_mock_transport_read_timeout_retries_then_succeeds() -> None:
    """Read timeout is treated as transport error, retried, then succeeds."""

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ReadTimeout("read timed out")
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "qwen3-6-27b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    client = ServerlessClient(
        base_url=BASE_URL,
        api_key="test-key",
        model="qwen3-6-27b",
        retry_delays=(0.0, 0.0, 0.0),
        sleep=_completed_sleep,
        transport=httpx.MockTransport(handler),
    )

    result = await client.chat_completion(messages=[{"role": "user", "content": "x"}], max_tokens=4)
    await client.aclose()

    assert result.content == "ok"
    assert call_count == 2
