"""Tests for RunPod endpoint admin helpers."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
import respx

from pitwall.runpod_client.endpoints import hibernate_endpoint
from pitwall.runpod_client.pods import RunPodError, RunPodRestError

ENDPOINT_ID = "abc123"


@respx.mock
def test_hibernate_endpoint_patches_workers_min_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_auth: list[str | None] = []
    captured_body: list[dict[str, Any]] = []
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        captured_auth.append(request.headers.get("authorization"))
        captured_body.append(cast(dict[str, Any], json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "id": ENDPOINT_ID,
                "workersMin": 0,
                "workersMax": 3,
            },
        )

    route = respx.patch(f"https://rest.runpod.io/v1/endpoints/{ENDPOINT_ID}").mock(
        side_effect=handler
    )

    result = hibernate_endpoint(ENDPOINT_ID)

    assert result["id"] == ENDPOINT_ID
    assert result["workersMin"] == 0
    assert captured_auth == ["Bearer test-key"]
    assert captured_body == [{"workersMin": 0}]
    assert route.call_count == 1


@respx.mock
def test_hibernate_endpoint_respects_rest_api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("RUNPOD_REST_API_URL", "https://runpod.example.test/api/")
    route = respx.patch(f"https://runpod.example.test/api/endpoints/{ENDPOINT_ID}").mock(
        return_value=httpx.Response(200, json={"id": ENDPOINT_ID, "workersMin": 0})
    )

    result = hibernate_endpoint(f" /{ENDPOINT_ID}/ ")

    assert result == {"id": ENDPOINT_ID, "workersMin": 0}
    assert route.call_count == 1


def test_hibernate_endpoint_rejects_empty_endpoint_id() -> None:
    with pytest.raises(ValueError, match="endpoint_id must be non-empty"):
        hibernate_endpoint(" / ")


def test_hibernate_endpoint_rejects_path_like_endpoint_id() -> None:
    with pytest.raises(ValueError, match="endpoint_id must not contain path separators"):
        hibernate_endpoint("endpoint-1/status")


def test_hibernate_endpoint_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

    with pytest.raises(RunPodError, match="RUNPOD_API_KEY not set"):
        hibernate_endpoint(ENDPOINT_ID)


@respx.mock
def test_hibernate_endpoint_raises_rest_error_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.patch(f"https://rest.runpod.io/v1/endpoints/{ENDPOINT_ID}").mock(
        return_value=httpx.Response(401, text='{"error":"unauthorized"}')
    )

    with pytest.raises(RunPodRestError) as exc_info:
        hibernate_endpoint(ENDPOINT_ID)

    assert exc_info.value.method == "PATCH"
    assert exc_info.value.path == f"endpoints/{ENDPOINT_ID}"
    assert exc_info.value.status_code == 401
    assert exc_info.value.body == '{"error":"unauthorized"}'
