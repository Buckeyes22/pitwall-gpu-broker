"""RunPod Serverless OpenAI-compatible client + endpoint admin.

Async httpx wrapper around the chat-completions endpoint exposed by RunPod
Serverless workers configured by the operator. Pitwall does not publish a GPU
worker image in the public alpha.

Also provides async CRUD operations for RunPod serverless endpoint management
(workers min/max, idle timeout, GPU type, flashboot) via the REST API.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from pitwall.rate_limits.retry_after import (
    DEFAULT_MAX_RETRY_AFTER_DELAY_S,
    parse_retry_after,
)
from pitwall.runpod_client.pods import RunPodError, RunPodRestError

SleepFunc = Callable[[float], Awaitable[object]]
ClockFunc = Callable[[], dt.datetime]


class ServerlessResponse(BaseModel):
    """Parsed chat-completion response."""

    content: str
    model: str
    input_tokens: int
    output_tokens: int
    finish_reason: str
    duration_ms: int
    raw: dict[str, Any]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class ServerlessClient:
    """OpenAI-compatible /v1/chat/completions client for RunPod Serverless."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: int = 600,
        retry_delays: tuple[float, ...] = (1.0, 3.0, 9.0),
        max_retry_after_s: float = DEFAULT_MAX_RETRY_AFTER_DELAY_S,
        sleep: SleepFunc = asyncio.sleep,
        clock: ClockFunc = _utc_now,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.endswith("/openai/v1"):
            raise ValueError(f"base_url must end with /openai/v1; got {base_url!r}")
        if max_retry_after_s < 0:
            raise ValueError("max_retry_after_s must be >= 0")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            transport=transport,
        )
        self._model = model
        self._retry_delays = retry_delays
        self._max_retry_after_s = max_retry_after_s
        self._sleep = sleep
        self._clock = clock

    @property
    def model(self) -> str:
        return self._model

    async def chat_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float = 0.0,
        extra: dict[str, Any] | None = None,
    ) -> ServerlessResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if extra:
            payload.update(extra)

        t0 = time.perf_counter()
        response = await self._retry_post("/chat/completions", json=payload)
        data = response.json()
        duration_ms = max(1, int((time.perf_counter() - t0) * 1000))

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {}) or {}
        usage = data.get("usage", {}) or {}

        return ServerlessResponse(
            content=str(message.get("content", "")),
            model=str(data.get("model", self._model)),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            finish_reason=str(choice.get("finish_reason", "")),
            duration_ms=duration_ms,
            raw=data,
        )

    async def _retry_post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
        attempts = len(self._retry_delays) + 1
        next_delay_s = 0.0
        last_error: Exception | None = None

        for attempt_index in range(attempts):
            if next_delay_s:
                await self._sleep(next_delay_s)

            try:
                response = await self._client.post(path, json=json)
            except httpx.HTTPError as exc:
                last_error = exc
                next_delay_s = self._next_configured_delay(attempt_index)
                continue

            if response.status_code == 429:
                last_error = _status_error(response, "Rate limited")
                next_delay_s = self._retry_after_delay(response, attempt_index)
                continue

            if response.status_code < 500:
                response.raise_for_status()
                return response

            last_error = _status_error(response, f"Server error: {response.status_code}")
            next_delay_s = self._next_configured_delay(attempt_index)

        if last_error is not None:
            raise last_error
        raise RuntimeError("serverless request failed without response or exception")

    def _retry_after_delay(self, response: httpx.Response, attempt_index: int) -> float:
        retry_after_delay = parse_retry_after(
            response.headers.get("Retry-After"),
            now=self._clock(),
            max_delay_s=self._max_retry_after_s,
        )
        if retry_after_delay is not None:
            return retry_after_delay
        return self._next_configured_delay(attempt_index)

    def _next_configured_delay(self, attempt_index: int) -> float:
        if attempt_index >= len(self._retry_delays):
            return 0.0
        return self._retry_delays[attempt_index]

    async def aclose(self) -> None:
        await self._client.aclose()


def _status_error(response: httpx.Response, message: str) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        message=message,
        request=response.request,
        response=response,
    )


class EndpointScalingConfig(BaseModel):
    """Scaling configuration for a RunPod serverless endpoint.

    Attributes:
        workers_min: Minimum number of idle workers kept warm (default 0).
        workers_max: Maximum number of concurrent workers (default 3).
        idle_timeout: Seconds before an idle worker is stopped (default 60).
        gpu_type_id: GPU type identifier (e.g. "NVIDIA L4"). If not set,
            RunPod auto-selects based on availability.
        flashboot: If True, enable flashboot for faster cold-start.
    """

    model_config = ConfigDict(extra="forbid")

    workers_min: int = Field(default=0, ge=0)
    workers_max: int = Field(default=3, ge=1)
    idle_timeout: int = Field(default=60, ge=0)
    gpu_type_id: str | None = None
    flashboot: bool = False

    def to_request_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "workersMin": self.workers_min,
            "workersMax": self.workers_max,
            "idleTimeout": self.idle_timeout,
        }
        if self.gpu_type_id is not None:
            payload["gpuTypeId"] = self.gpu_type_id
        if self.flashboot:
            payload["flashboot"] = True
        return payload


class Endpoint(BaseModel):
    """A RunPod serverless endpoint."""

    id: str
    name: str
    scaling: EndpointScalingConfig
    template_id: str | None = None
    created_at: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


_REST_BASE_URL = "https://rest.runpod.io/v1"
_REST_TIMEOUT_S = 60.0


def _rest_api_key() -> str:
    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        raise RunPodError("RUNPOD_API_KEY not set in process env")
    return key


def _rest_base_url() -> str:
    return os.environ.get("RUNPOD_REST_API_URL", _REST_BASE_URL).rstrip("/")


async def _rest_request_async(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: float = _REST_TIMEOUT_S,
) -> Any:
    url = f"{_rest_base_url()}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {_rest_api_key()}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.request(
            method,
            url,
            headers=headers,
            json=json_body,
            params=params,
        )
    if response.status_code == 204:
        return {}
    if response.status_code >= 400:
        raise RunPodRestError(method, path, response.status_code, response.text)
    if not response.content:
        return {}
    return response.json()


def _normalize_endpoint_id(endpoint_id: str) -> str:
    normalized = endpoint_id.strip().strip("/")
    if not normalized:
        raise ValueError("endpoint_id must be non-empty")
    if "/" in normalized:
        raise ValueError("endpoint_id must not contain path separators")
    return normalized


def _parse_endpoint(data: dict[str, Any]) -> Endpoint:
    scaling_data = data.get("scaling", {})
    if isinstance(scaling_data, dict):
        scaling = EndpointScalingConfig(
            workers_min=scaling_data.get("workersMin", 0),
            workers_max=scaling_data.get("workersMax", 3),
            idle_timeout=scaling_data.get("idleTimeout", 60),
            gpu_type_id=scaling_data.get("gpuTypeId"),
            flashboot=scaling_data.get("flashboot", False),
        )
    else:
        scaling = EndpointScalingConfig()
    return Endpoint(
        id=str(data.get("id", "")),
        name=str(data.get("name", "")),
        scaling=scaling,
        template_id=data.get("templateId"),
        created_at=data.get("createdAt"),
        raw=dict(data),
    )


async def create_endpoint(
    name: str,
    template_id: str | None,
    *,
    gpu_ids: list[str] | None = None,
    scaling: EndpointScalingConfig | None = None,
    timeout_s: float = _REST_TIMEOUT_S,
) -> Endpoint:
    """Create a RunPod serverless endpoint.

    Args:
        name: Human-readable endpoint name.
        template_id: RunPod template ID to deploy.
        gpu_ids: List of GPU type IDs to restrict (e.g. ["NVIDIA L4"]).
            RunPod selects one if not specified.
        scaling: Scaling configuration (workers min/max, idle timeout, GPU,
            flashboot). Defaults to EndpointScalingConfig() if not provided.
        timeout_s: Request timeout in seconds.

    Returns:
        The created Endpoint.
    """
    scaling_config = scaling if scaling is not None else EndpointScalingConfig()
    payload: dict[str, Any] = {
        "name": name,
        "scaling": scaling_config.to_request_json(),
    }
    if template_id is not None:
        payload["templateId"] = template_id
    if gpu_ids is not None:
        payload["gpuIds"] = gpu_ids
    result = await _rest_request_async(
        "POST",
        "endpoints",
        json_body=payload,
        timeout_s=timeout_s,
    )
    if not isinstance(result, dict):
        raise RunPodError(f"create_endpoint({name}) returned unexpected shape: {result!r}")
    return _parse_endpoint(result)


async def get_endpoint(
    endpoint_id: str,
    *,
    timeout_s: float = _REST_TIMEOUT_S,
) -> Endpoint:
    """Fetch a single serverless endpoint by ID.

    Args:
        endpoint_id: RunPod endpoint ID.
        timeout_s: Request timeout in seconds.

    Returns:
        The Endpoint.
    """
    normalized = _normalize_endpoint_id(endpoint_id)
    result = await _rest_request_async(
        "GET",
        f"endpoints/{normalized}",
        params={"include": ["machine"]},
        timeout_s=timeout_s,
    )
    if not isinstance(result, dict):
        raise RunPodError(f"get_endpoint({normalized}) returned unexpected shape: {result!r}")
    return _parse_endpoint(result)


async def list_endpoints(
    *,
    name_prefix: str | None = None,
    timeout_s: float = _REST_TIMEOUT_S,
) -> list[Endpoint]:
    """List all serverless endpoints for the account.

    Args:
        name_prefix: Optional filter for endpoint names starting with this prefix.
        timeout_s: Request timeout in seconds.

    Returns:
        List of Endpoints, possibly empty.
    """
    params: dict[str, Any] | None = None
    if name_prefix is not None:
        params = {"name": name_prefix}
    result = await _rest_request_async(
        "GET",
        "endpoints",
        params=params,
        timeout_s=timeout_s,
    )
    if not isinstance(result, list):
        raise RunPodError(f"list_endpoints returned unexpected shape: {result!r}")
    return [_parse_endpoint(item) for item in result if isinstance(item, dict)]


async def update_endpoint_scaling(
    endpoint_id: str,
    scaling: EndpointScalingConfig,
    *,
    timeout_s: float = _REST_TIMEOUT_S,
) -> Endpoint:
    """Update the scaling configuration for a serverless endpoint.

    Args:
        endpoint_id: RunPod endpoint ID.
        scaling: New scaling configuration.
        timeout_s: Request timeout in seconds.

    Returns:
        The updated Endpoint.
    """
    normalized = _normalize_endpoint_id(endpoint_id)
    result = await _rest_request_async(
        "PATCH",
        f"endpoints/{normalized}",
        json_body={"scaling": scaling.to_request_json()},
        timeout_s=timeout_s,
    )
    if not isinstance(result, dict):
        raise RunPodError(
            f"update_endpoint_scaling({normalized}) returned unexpected shape: {result!r}"
        )
    return _parse_endpoint(result)


async def delete_endpoint(
    endpoint_id: str,
    *,
    timeout_s: float = _REST_TIMEOUT_S,
) -> dict[str, Any]:
    """Delete a serverless endpoint.

    Args:
        endpoint_id: RunPod endpoint ID.
        timeout_s: Request timeout in seconds.

    Returns:
        Empty dict on success.
    """
    normalized = _normalize_endpoint_id(endpoint_id)
    result = await _rest_request_async(
        "DELETE",
        f"endpoints/{normalized}",
        timeout_s=timeout_s,
    )
    if not isinstance(result, dict):
        return {}
    return result


__all__ = [
    "DEFAULT_MAX_RETRY_AFTER_DELAY_S",
    "Endpoint",
    "EndpointScalingConfig",
    "RunPodError",
    "RunPodRestError",
    "ServerlessClient",
    "ServerlessResponse",
    "create_endpoint",
    "delete_endpoint",
    "get_endpoint",
    "list_endpoints",
    "parse_retry_after",
    "update_endpoint_scaling",
]
