"""Tests for pitwall.workers.vllm — VLLMForwarder pass-through behavior."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pitwall.workers.vllm import VLLMForwarder
from tests.fakes.runpod import RunPodServerlessFake

pytestmark = pytest.mark.anyio


async def test_chat_completion_forwards_standard_fields(
    runpod_serverless_fake: RunPodServerlessFake,
) -> None:
    """VLLMForwarder.chat_completion forwards model, messages, max_tokens, temperature, stream."""
    runpod_serverless_fake.add_chat_completion("hi")

    forwarder = VLLMForwarder(
        base_url="http://127.0.0.1:8000",
        timeout_s=300,
        transport=runpod_serverless_fake.transport(),
    )
    response = await forwarder.chat_completion(
        messages=[{"role": "user", "content": "hello"}],
        model="qwen3-32b",
        max_tokens=512,
        temperature=0.7,
        stream=False,
    )

    assert response.status_code == 200
    request = runpod_serverless_fake.requests[0]
    body: dict[str, Any] = json.loads(request.content)
    assert body["model"] == "qwen3-32b"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.7
    assert body["stream"] is False
    await forwarder.aclose()


async def test_chat_completion_passthrough_extra_fields(
    runpod_serverless_fake: RunPodServerlessFake,
) -> None:
    """extra dict fields (tools, tool_calls, frequency_penalty, etc.) are passed through verbatim."""
    extra_response = {
        "id": "chatcmpl-test",
        "model": "qwen3-32b",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "get_weather", "arguments": '{"city":"KC"}'},
                            "type": "function",
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 7,
            "total_tokens": 12,
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["frequency_penalty"] == 1.0
        assert body["presence_penalty"] == 0.5
        assert body["top_p"] == 0.9
        assert body["stop"] == ["END"]
        assert "tools" in body
        assert len(body["tools"]) == 1
        assert body["tools"][0]["function"]["name"] == "get_weather"
        assert "tool_calls" in body
        assert body["tool_calls"][0]["function"]["name"] == "get_weather"
        return httpx.Response(200, json=extra_response, request=request)

    runpod_serverless_fake.add_handler(handler)

    forwarder = VLLMForwarder(
        base_url="http://127.0.0.1:8000",
        timeout_s=300,
        transport=runpod_serverless_fake.transport(),
    )
    response = await forwarder.chat_completion(
        messages=[{"role": "user", "content": "what is the weather in KC?"}],
        model="qwen3-32b",
        max_tokens=256,
        extra={
            "frequency_penalty": 1.0,
            "presence_penalty": 0.5,
            "top_p": 0.9,
            "stop": ["END"],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather for a city",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city":"KC"}',
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    tool_call = data["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "get_weather"
    await forwarder.aclose()


async def test_chat_completion_passthrough_response_shape(
    runpod_serverless_fake: RunPodServerlessFake,
) -> None:
    """The raw httpx.Response preserves the full OpenAI-compatible response shape."""
    raw_response = {
        "id": "chatcmpl-abc123",
        "model": "qwen3-32b",
        "created": 1234567890,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "The weather is sunny.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
        "system_fingerprint": "fp_123",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=raw_response, request=request)

    runpod_serverless_fake.add_handler(handler)

    forwarder = VLLMForwarder(
        base_url="http://127.0.0.1:8000",
        timeout_s=300,
        transport=runpod_serverless_fake.transport(),
    )
    response = await forwarder.chat_completion(
        messages=[{"role": "user", "content": "hello"}],
        model="qwen3-32b",
        max_tokens=256,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "chatcmpl-abc123"
    assert data["created"] == 1234567890
    assert data["system_fingerprint"] == "fp_123"
    assert data["choices"][0]["message"]["content"] == "The weather is sunny."
    assert data["usage"]["total_tokens"] == 15
    await forwarder.aclose()


async def test_chat_completion_passthrough_stream_response(
    runpod_serverless_fake: RunPodServerlessFake,
) -> None:
    """Streaming responses are returned verbatim (SSE bytes)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        sse_data = (
            b'data: {"choices":[{"index":0,"delta":{"content":"Hello"}}]}\n\n'
            b'data: {"choices":[{"index":0,"delta":{"content":" world"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            content=sse_data,
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    runpod_serverless_fake.add_handler(handler)

    forwarder = VLLMForwarder(
        base_url="http://127.0.0.1:8000",
        timeout_s=300,
        transport=runpod_serverless_fake.transport(),
    )
    response = await forwarder.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        model="qwen3-32b",
        max_tokens=256,
        stream=True,
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert b"Hello" in response.content
    assert b"world" in response.content
    await forwarder.aclose()


async def test_chat_completion_post_to_v1_chat_completions(
    runpod_serverless_fake: RunPodServerlessFake,
) -> None:
    """Requests are POSTed to /v1/chat/completions."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            request=request,
        )

    runpod_serverless_fake.add_handler(handler)

    forwarder = VLLMForwarder(
        base_url="http://127.0.0.1:8000",
        timeout_s=300,
        transport=runpod_serverless_fake.transport(),
    )
    response = await forwarder.chat_completion(
        messages=[{"role": "user", "content": "test"}],
        model="test",
        max_tokens=10,
    )

    assert response.status_code == 200
    await forwarder.aclose()


async def test_aclose_closes_underlying_client() -> None:
    """aclose() closes the internal httpx.AsyncClient without error."""
    forwarder = VLLMForwarder(base_url="http://127.0.0.1:8000", timeout_s=300)
    await forwarder.aclose()


async def test_extra_none_does_not_modify_payload(
    runpod_serverless_fake: RunPodServerlessFake,
) -> None:
    """When extra is None, only standard fields are sent (no 'extra' key in payload)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "extra" not in body
        assert "frequency_penalty" not in body
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            request=request,
        )

    runpod_serverless_fake.add_handler(handler)

    forwarder = VLLMForwarder(
        base_url="http://127.0.0.1:8000",
        timeout_s=300,
        transport=runpod_serverless_fake.transport(),
    )
    response = await forwarder.chat_completion(
        messages=[{"role": "user", "content": "test"}],
        model="test",
        max_tokens=10,
        extra=None,
    )

    assert response.status_code == 200
    await forwarder.aclose()
