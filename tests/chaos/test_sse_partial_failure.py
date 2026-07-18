"""Chaos: partial SSE failure emits an error frame and closes the stream."""

from __future__ import annotations

import json

import httpx
import pytest

from pitwall.api.routes.openai import _relay_upstream_bytes
from tests.conftest import FailingAsyncByteStream

pytestmark = [pytest.mark.anyio, pytest.mark.chaos]


def _sse(chunks: list[dict]) -> list[bytes]:
    return [f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks]


async def test_partial_sse_failure_closes_stream() -> None:
    chunks = _sse(
        [
            {"choices": [{"delta": {"content": "Hi"}}]},
            {"choices": [{"delta": {"content": "!"}}]},
        ]
    )
    stream = FailingAsyncByteStream(chunks, fail_after=1)
    upstream_response = httpx.Response(
        200,
        stream=stream,
        headers={"content-type": "text/event-stream"},
    )
    client = httpx.AsyncClient()

    relayed: list[bytes] = []
    async for chunk in _relay_upstream_bytes(upstream_response, client):
        relayed.append(chunk)

    data = b"".join(relayed)
    assert chunks[0] in data
    assert b'"error"' in data
    assert b"upstream stream failure" in data
    assert stream.closed
    assert upstream_response.is_closed
    assert client.is_closed
