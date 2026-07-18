"""Tests for RunPod serverless endpoint CRUD + scaling config."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
import respx

from pitwall.runpod_client.pods import RunPodRestError
from pitwall.runpod_client.serverless import (
    Endpoint,
    EndpointScalingConfig,
    create_endpoint,
    delete_endpoint,
    get_endpoint,
    list_endpoints,
    update_endpoint_scaling,
)

pytestmark = pytest.mark.anyio

ENDPOINT_ID = "abc123"
REST_BASE = "https://rest.runpod.io/v1"


class TestEndpointScalingConfig:
    def test_defaults(self) -> None:
        config = EndpointScalingConfig()
        assert config.workers_min == 0
        assert config.workers_max == 3
        assert config.idle_timeout == 60
        assert config.gpu_type_id is None
        assert config.flashboot is False

    def test_custom_values(self) -> None:
        config = EndpointScalingConfig(
            workers_min=1,
            workers_max=5,
            idle_timeout=120,
            gpu_type_id="NVIDIA H100",
            flashboot=True,
        )
        assert config.workers_min == 1
        assert config.workers_max == 5
        assert config.idle_timeout == 120
        assert config.gpu_type_id == "NVIDIA H100"
        assert config.flashboot is True

    def test_to_request_json_full(self) -> None:
        config = EndpointScalingConfig(
            workers_min=2,
            workers_max=4,
            idle_timeout=90,
            gpu_type_id="NVIDIA L4",
            flashboot=True,
        )
        payload = config.to_request_json()
        assert payload == {
            "workersMin": 2,
            "workersMax": 4,
            "idleTimeout": 90,
            "gpuTypeId": "NVIDIA L4",
            "flashboot": True,
        }

    def test_to_request_json_omits_optional_gpu_when_none(self) -> None:
        config = EndpointScalingConfig(gpu_type_id=None)
        payload = config.to_request_json()
        assert "gpuTypeId" not in payload

    def test_to_request_json_omits_flashboot_when_false(self) -> None:
        config = EndpointScalingConfig(flashboot=False)
        payload = config.to_request_json()
        assert "flashboot" not in payload

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValueError):
            EndpointScalingConfig(workers_min=0, extra_field=True)  # type: ignore[call-arg]  # reason: intentionally forbidden extra field

    def test_workers_min_negative_rejected(self) -> None:
        with pytest.raises(ValueError):
            EndpointScalingConfig(workers_min=-1)

    def test_workers_max_less_than_one_rejected(self) -> None:
        with pytest.raises(ValueError):
            EndpointScalingConfig(workers_max=0)

    def test_round_trip_from_request_json(self) -> None:
        original = EndpointScalingConfig(
            workers_min=1,
            workers_max=6,
            idle_timeout=45,
            gpu_type_id="NVIDIA H100",
            flashboot=True,
        )
        payload = original.to_request_json()
        parsed = EndpointScalingConfig(
            workers_min=payload["workersMin"],
            workers_max=payload["workersMax"],
            idle_timeout=payload["idleTimeout"],
            gpu_type_id=payload.get("gpuTypeId"),
            flashboot=payload.get("flashboot", False),
        )
        assert parsed.workers_min == original.workers_min
        assert parsed.workers_max == original.workers_max
        assert parsed.idle_timeout == original.idle_timeout
        assert parsed.gpu_type_id == original.gpu_type_id
        assert parsed.flashboot == original.flashboot


@respx.mock
async def test_create_endpoint_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    runpod_response_factory: Any,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = cast(dict[str, Any], json.loads(request.content))
        response: httpx.Response = runpod_response_factory.endpoint_response(endpoint_id="new-ep-1")
        return response

    respx.post(f"{REST_BASE}/endpoints").mock(side_effect=handler)

    scaling = EndpointScalingConfig(workers_min=1, workers_max=4, idle_timeout=90)
    result = await create_endpoint(
        name="my-endpoint",
        template_id="tmpl-abc",
        gpu_ids=["NVIDIA L4"],
        scaling=scaling,
    )

    assert isinstance(result, Endpoint)
    assert result.id == "new-ep-1"
    assert result.name == "test-endpoint"
    assert result.scaling.workers_min == 0
    assert result.scaling.workers_max == 3
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"]["name"] == "my-endpoint"
    assert captured["body"]["templateId"] == "tmpl-abc"
    assert captured["body"]["gpuIds"] == ["NVIDIA L4"]
    assert captured["body"]["scaling"]["workersMin"] == 1
    assert captured["body"]["scaling"]["workersMax"] == 4
    assert captured["body"]["scaling"]["idleTimeout"] == 90


@respx.mock
async def test_create_endpoint_defaults_scaling_when_not_provided(
    monkeypatch: pytest.MonkeyPatch,
    runpod_response_factory: Any,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = cast(dict[str, Any], json.loads(request.content))
        response: httpx.Response = runpod_response_factory.endpoint_response()
        return response

    respx.post(f"{REST_BASE}/endpoints").mock(side_effect=handler)

    result = await create_endpoint(name="my-ep", template_id=None)

    assert isinstance(result, Endpoint)
    assert captured["body"]["scaling"]["workersMin"] == 0
    assert captured["body"]["scaling"]["workersMax"] == 3
    assert captured["body"]["scaling"]["idleTimeout"] == 60
    assert "gpuTypeId" not in captured["body"]["scaling"]


@respx.mock
async def test_create_endpoint_raises_rest_error_on_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.post(f"{REST_BASE}/endpoints").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )

    with pytest.raises(RunPodRestError) as exc_info:
        await create_endpoint(name="ep", template_id=None)

    assert exc_info.value.method == "POST"
    assert exc_info.value.path == "endpoints"
    assert exc_info.value.status_code == 401


@respx.mock
async def test_create_endpoint_uses_custom_rest_url(
    monkeypatch: pytest.MonkeyPatch,
    runpod_response_factory: Any,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("RUNPOD_REST_API_URL", "https://runpod.example.test/api/")
    route = respx.post("https://runpod.example.test/api/endpoints").mock(
        return_value=runpod_response_factory.endpoint_response(endpoint_id="ep-x")
    )

    result = await create_endpoint(name="ep-x", template_id=None)

    assert result.id == "ep-x"
    assert route.call_count == 1


@respx.mock
async def test_create_endpoint_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    respx.post(f"{REST_BASE}/endpoints").mock(
        return_value=httpx.Response(200, json={"error": "unauthorized"})
    )

    with pytest.raises(RuntimeError, match="RUNPOD_API_KEY not set"):
        await create_endpoint(name="ep", template_id=None)


@respx.mock
async def test_get_endpoint_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    runpod_response_factory: Any,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        response: httpx.Response = runpod_response_factory.endpoint_response(
            endpoint_id=ENDPOINT_ID,
            name="my-ep",
            workers_min=1,
            workers_max=5,
            idle_timeout=90,
            gpu_type_id="NVIDIA H100",
            flashboot=True,
        )
        return response

    respx.get(f"{REST_BASE}/endpoints/{ENDPOINT_ID}").mock(side_effect=handler)

    result = await get_endpoint(ENDPOINT_ID)

    assert isinstance(result, Endpoint)
    assert result.id == ENDPOINT_ID
    assert result.name == "my-ep"
    assert result.scaling.workers_min == 1
    assert result.scaling.workers_max == 5
    assert result.scaling.idle_timeout == 90
    assert result.scaling.gpu_type_id == "NVIDIA H100"
    assert result.scaling.flashboot is True
    assert captured["auth"] == "Bearer test-key"


@respx.mock
async def test_get_endpoint_normalizes_id(
    monkeypatch: pytest.MonkeyPatch,
    runpod_response_factory: Any,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    route = respx.get(f"{REST_BASE}/endpoints/{ENDPOINT_ID}").mock(
        return_value=runpod_response_factory.endpoint_response(endpoint_id=ENDPOINT_ID)
    )

    result = await get_endpoint(f"  /{ENDPOINT_ID}/  ")

    assert result.id == ENDPOINT_ID
    assert route.call_count == 1


@respx.mock
async def test_get_endpoint_raises_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.get(f"{REST_BASE}/endpoints/{ENDPOINT_ID}").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )

    with pytest.raises(RunPodRestError) as exc_info:
        await get_endpoint(ENDPOINT_ID)

    assert exc_info.value.status_code == 404


async def test_get_endpoint_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="endpoint_id must be non-empty"):
        await get_endpoint("  ")


async def test_get_endpoint_rejects_path_separator_in_id() -> None:
    with pytest.raises(ValueError, match="endpoint_id must not contain path separators"):
        await get_endpoint("ep-1/status")


@respx.mock
async def test_list_endpoints_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    runpod_response_factory: Any,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.get(f"{REST_BASE}/endpoints").mock(
        return_value=httpx.Response(
            200,
            json=[
                runpod_response_factory.endpoint(endpoint_id="ep-1", name="ep-one"),
                runpod_response_factory.endpoint(endpoint_id="ep-2", name="ep-two"),
            ],
        )
    )

    result = await list_endpoints()

    assert len(result) == 2
    assert result[0].id == "ep-1"
    assert result[0].name == "ep-one"
    assert result[1].id == "ep-2"
    assert result[1].name == "ep-two"


@respx.mock
async def test_list_endpoints_with_name_prefix(
    monkeypatch: pytest.MonkeyPatch,
    runpod_response_factory: Any,
) -> None:
    captured_params: dict[str, Any] = {}
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        response = httpx.Response(200, json=[runpod_response_factory.endpoint()])
        return response

    respx.get(f"{REST_BASE}/endpoints").mock(side_effect=handler)

    await list_endpoints(name_prefix="pitwall-")

    assert captured_params.get("name") == "pitwall-"


@respx.mock
async def test_list_endpoints_raises_on_non_list_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.get(f"{REST_BASE}/endpoints").mock(
        return_value=httpx.Response(200, json={"error": "oops"})
    )

    with pytest.raises(RuntimeError, match="list_endpoints returned unexpected shape"):
        await list_endpoints()


@respx.mock
async def test_list_endpoints_skips_non_dict_items(
    monkeypatch: pytest.MonkeyPatch,
    runpod_response_factory: Any,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.get(f"{REST_BASE}/endpoints").mock(
        return_value=httpx.Response(
            200,
            json=[
                runpod_response_factory.endpoint(endpoint_id="ep-1"),
                "not-a-dict",
                None,
                runpod_response_factory.endpoint(endpoint_id="ep-2"),
            ],
        )
    )

    result = await list_endpoints()

    assert len(result) == 2
    assert result[0].id == "ep-1"
    assert result[1].id == "ep-2"


@respx.mock
async def test_update_scaling_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    runpod_response_factory: Any,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = cast(dict[str, Any], json.loads(request.content))
        response: httpx.Response = runpod_response_factory.endpoint_response(
            endpoint_id=ENDPOINT_ID,
            workers_min=5,
            workers_max=10,
            idle_timeout=300,
            flashboot=True,
        )
        return response

    respx.patch(f"{REST_BASE}/endpoints/{ENDPOINT_ID}").mock(side_effect=handler)

    new_scaling = EndpointScalingConfig(
        workers_min=5,
        workers_max=10,
        idle_timeout=300,
        flashboot=True,
    )
    result = await update_endpoint_scaling(ENDPOINT_ID, scaling=new_scaling)

    assert isinstance(result, Endpoint)
    assert result.id == ENDPOINT_ID
    assert result.scaling.workers_min == 5
    assert result.scaling.workers_max == 10
    assert result.scaling.idle_timeout == 300
    assert result.scaling.flashboot is True
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"]["scaling"]["workersMin"] == 5
    assert captured["body"]["scaling"]["workersMax"] == 10
    assert captured["body"]["scaling"]["idleTimeout"] == 300
    assert captured["body"]["scaling"]["flashboot"] is True


@respx.mock
async def test_update_scaling_rejects_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.patch(f"{REST_BASE}/endpoints/{ENDPOINT_ID}").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )

    with pytest.raises(RunPodRestError) as exc_info:
        await update_endpoint_scaling(ENDPOINT_ID, scaling=EndpointScalingConfig())

    assert exc_info.value.status_code == 403


@respx.mock
async def test_delete_endpoint_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_auth: list[str] = []
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        captured_auth.append(request.headers.get("authorization", ""))
        return httpx.Response(204)

    respx.delete(f"{REST_BASE}/endpoints/{ENDPOINT_ID}").mock(side_effect=handler)

    result = await delete_endpoint(ENDPOINT_ID)

    assert result == {}
    assert captured_auth == ["Bearer test-key"]


@respx.mock
async def test_delete_endpoint_normalizes_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    route = respx.delete(f"{REST_BASE}/endpoints/{ENDPOINT_ID}").mock(
        return_value=httpx.Response(204)
    )

    await delete_endpoint(f"  /{ENDPOINT_ID}/  ")

    assert route.call_count == 1


@respx.mock
async def test_delete_endpoint_raises_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.delete(f"{REST_BASE}/endpoints/{ENDPOINT_ID}").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )

    with pytest.raises(RunPodRestError) as exc_info:
        await delete_endpoint(ENDPOINT_ID)

    assert exc_info.value.status_code == 404


async def test_delete_endpoint_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="endpoint_id must be non-empty"):
        await delete_endpoint("  ")


async def test_delete_endpoint_rejects_path_separator_in_id() -> None:
    with pytest.raises(ValueError, match="endpoint_id must not contain path separators"):
        await delete_endpoint("ep-1/subpath")
