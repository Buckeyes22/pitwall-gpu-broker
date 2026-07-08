"""Chaos: serverless 5xx is retried, then surfaced."""

from __future__ import annotations

import httpx
import pytest

from pitwall.runpod_client.serverless import ServerlessClient
from tests.fakes.runpod import RunPodServerlessFake

pytestmark = [pytest.mark.anyio, pytest.mark.chaos]


async def _noop_sleep(_seconds: float) -> None:
    return None


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "c1",
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _client(fake: RunPodServerlessFake, *, retry_delays=(0.0,)) -> ServerlessClient:
    return ServerlessClient(
        base_url="http://test/openai/v1",
        api_key="k",
        model="m",
        retry_delays=retry_delays,
        sleep=_noop_sleep,
        transport=fake.transport(),
    )


async def test_503_then_200_is_retried() -> None:
    fake = RunPodServerlessFake()
    fake.add_response(httpx.Response(503, json={"e": "x"}))
    fake.add_response(_ok_response())
    client = _client(fake)
    try:
        resp = await client.chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=4,
        )
    finally:
        await client.aclose()

    assert resp.content == "OK"
    assert len(fake.requests) == 2


async def test_persistent_5xx_raises() -> None:
    fake = RunPodServerlessFake()
    fake.add_response(httpx.Response(500, json={"e": "x"}))
    client = _client(fake, retry_delays=())
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=4,
            )
    finally:
        await client.aclose()

    assert len(fake.requests) == 1
