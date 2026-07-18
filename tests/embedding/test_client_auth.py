"""httpx.MockTransport tests for ServerlessLBClient — direct RunPod auth and Pitwall mode routing."""

from __future__ import annotations

import httpx
import pytest

from pitwall.runpod_client.serverless_lb import ServerlessLBClient


@pytest.mark.asyncio
async def test_direct_mode_does_not_send_authorization_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"dense": [[0.0]], "sparse": [{}], "model": "test"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("PITWALL_EMBEDDING_VIA_PITWALL", "false")
    _clear_settings_cache()

    client = ServerlessLBClient(lb_base_url="http://embed.local")
    try:
        await client.embed(["hello"])
    finally:
        await client.aclose()

    assert seen_headers["authorization"] is None


@pytest.mark.asyncio
async def test_direct_mode_sends_bearer_authorization_when_api_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"dense": [[0.0]], "sparse": [{}], "model": "test"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("PITWALL_EMBEDDING_VIA_PITWALL", "false")
    _clear_settings_cache()

    client = ServerlessLBClient(lb_base_url="http://embed.local", api_key="x")
    try:
        await client.embed(["hello"])
    finally:
        await client.aclose()

    assert seen_headers["authorization"] == "Bearer x"


@pytest.mark.asyncio
async def test_direct_mode_rejects_payloads_over_runpod_limit_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_fired = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal http_fired
        http_fired = True
        return httpx.Response(200, json={"dense": [[0.0]], "sparse": [{}], "model": "test"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("PITWALL_EMBEDDING_VIA_PITWALL", "false")
    _clear_settings_cache()

    client = ServerlessLBClient(lb_base_url="http://embed.local")
    try:
        with pytest.raises(ValueError, match="30 MB.*chunk smaller"):
            await client.embed(["x" * 30_000_000])
    finally:
        await client.aclose()

    assert http_fired is False


@pytest.mark.asyncio
async def test_direct_mode_retries_on_502_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(200, json={"dense": [[0.0]], "sparse": [{}], "model": "test"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("PITWALL_EMBEDDING_VIA_PITWALL", "false")
    _clear_settings_cache()

    client = ServerlessLBClient(
        lb_base_url="http://embed.local", retry_backoff_s=0.0, retry_attempts=4
    )
    try:
        result = await client.embed(["hello"])
    finally:
        await client.aclose()

    assert call_count == 3
    assert result["raw"]["model"] == "test"


@pytest.mark.asyncio
async def test_direct_mode_gives_up_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, text="Service Unavailable")

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("PITWALL_EMBEDDING_VIA_PITWALL", "false")
    _clear_settings_cache()

    client = ServerlessLBClient(
        lb_base_url="http://embed.local", retry_attempts=3, retry_backoff_s=0.0
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.embed(["hello"])
    finally:
        await client.aclose()

    assert call_count == 3


@pytest.mark.asyncio
async def test_direct_mode_strips_trailing_embed_from_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"dense": [[0.0]], "sparse": [{}], "model": "test"})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("PITWALL_EMBEDDING_VIA_PITWALL", "false")
    _clear_settings_cache()

    for url in (
        "http://embed.local",
        "http://embed.local/",
        "http://embed.local/embed",
        "http://embed.local/embed/",
    ):
        client = ServerlessLBClient(lb_base_url=url)
        try:
            await client.embed(["hi"])
        finally:
            await client.aclose()

    assert len(seen_urls) == 4
    for u in seen_urls:
        assert u == "http://embed.local/embed", f"expected …/embed (single), got {u}"


def test_rejects_retry_attempts_below_1() -> None:
    with pytest.raises(ValueError, match="retry_attempts must be >= 1"):
        ServerlessLBClient(lb_base_url="http://embed.local", retry_attempts=0)
    with pytest.raises(ValueError, match="retry_attempts must be >= 1"):
        ServerlessLBClient(lb_base_url="http://embed.local", retry_attempts=-3)


def test_rejects_negative_retry_backoff() -> None:
    with pytest.raises(ValueError, match="retry_backoff_s must be >= 0"):
        ServerlessLBClient(lb_base_url="http://embed.local", retry_backoff_s=-1.0)


@pytest.mark.asyncio
async def test_pitwall_mode_routes_to_pitwall_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "result": {
                    "dense": [[0.0]],
                    "sparse": [{}],
                }
            },
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("PITWALL_EMBEDDING_VIA_PITWALL", "true")
    monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall.local")
    _clear_settings_cache()

    client = ServerlessLBClient(lb_base_url="http://embed.local")
    try:
        await client.embed(["hello"])
    finally:
        await client.aclose()

    assert len(seen_urls) == 1
    assert seen_urls[0] == "http://pitwall.local/v1/inference"


@pytest.mark.asyncio
async def test_pitwall_mode_sends_correct_payload_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen_bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "result": {
                    "dense": [[0.0]],
                    "sparse": [{}],
                }
            },
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("PITWALL_EMBEDDING_VIA_PITWALL", "true")
    monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall.local")
    _clear_settings_cache()

    client = ServerlessLBClient(lb_base_url="http://embed.local", api_key="test-key")
    try:
        await client.embed(
            ["hello", "world"],
            return_dense=True,
            return_sparse=True,
            return_colbert=False,
        )
    finally:
        await client.aclose()

    assert len(seen_bodies) == 1
    body = seen_bodies[0]
    assert body["capability"] == "embedding.bge-m3"
    assert body["texts"] == ["hello", "world"]
    assert body["return_dense"] is True
    assert body["return_sparse"] is True
    assert body["return_colbert"] is False


@pytest.mark.asyncio
async def test_pitwall_mode_sends_bearer_auth_to_pitwall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "result": {
                    "dense": [[0.0]],
                    "sparse": [{}],
                }
            },
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("PITWALL_EMBEDDING_VIA_PITWALL", "true")
    monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall.local")
    _clear_settings_cache()

    client = ServerlessLBClient(lb_base_url="http://embed.local", api_key="secret-key")
    try:
        await client.embed(["hello"])
    finally:
        await client.aclose()

    assert seen_headers["authorization"] == "Bearer secret-key"


@pytest.mark.asyncio
async def test_pitwall_mode_rejects_payloads_over_limit_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_fired = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal http_fired
        http_fired = True
        return httpx.Response(
            200,
            json={
                "result": {
                    "dense": [[0.0]],
                    "sparse": [{}],
                }
            },
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("PITWALL_EMBEDDING_VIA_PITWALL", "true")
    monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall.local")
    _clear_settings_cache()

    client = ServerlessLBClient(lb_base_url="http://embed.local")
    try:
        with pytest.raises(ValueError, match="30 MB.*chunk smaller"):
            await client.embed(["x" * 30_000_000])
    finally:
        await client.aclose()

    assert http_fired is False


@pytest.mark.asyncio
async def test_pitwall_mode_retries_on_502_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(
            200,
            json={
                "result": {
                    "dense": [[0.0]],
                    "sparse": [{}],
                }
            },
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("PITWALL_EMBEDDING_VIA_PITWALL", "true")
    monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall.local")
    _clear_settings_cache()

    client = ServerlessLBClient(
        lb_base_url="http://embed.local", retry_backoff_s=0.0, retry_attempts=4
    )
    try:
        result = await client.embed(["hello"])
    finally:
        await client.aclose()

    assert call_count == 3
    assert result["dense"] == [[0.0]]


@pytest.mark.asyncio
async def test_pitwall_mode_gives_up_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, text="Service Unavailable")

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setenv("PITWALL_EMBEDDING_VIA_PITWALL", "true")
    monkeypatch.setenv("PITWALL_BASE_URL", "http://pitwall.local")
    _clear_settings_cache()

    client = ServerlessLBClient(
        lb_base_url="http://embed.local", retry_attempts=3, retry_backoff_s=0.0
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.embed(["hello"])
    finally:
        await client.aclose()

    assert call_count == 3


def _clear_settings_cache() -> None:
    from pitwall.config import get_settings

    get_settings.cache_clear()
