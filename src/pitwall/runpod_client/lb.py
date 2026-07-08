"""RunPod Load-Balancing Serverless client.

Wraps the LB surface that routes HTTP directly to worker pods:

    https://{ENDPOINT_ID}.api.runpod.ai/{CUSTOM_PATH}

Workers expose ``/ping`` on ``PORT_HEALTH``. LB endpoints have no built-in
retry, queue, or backpressure — Pitwall treats them as low-latency real-time
providers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel

from pitwall.rate_limits.retry_after import (
    DEFAULT_MAX_RETRY_AFTER_DELAY_S,
    parse_retry_after,
)

DEFAULT_PROBE_TIMEOUT_S = 5.0

SleepFunc = Callable[[float], Awaitable[object]]
ClockFunc = Callable[[], dt.datetime]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class ProbeResult(BaseModel):
    """Structured result from a provider health probe.

    Attributes:
        healthy: True if the probe passed (2xx response).
        status_code: HTTP status code if available, None on timeout/connection error.
        error: Error type string if probe failed (e.g., "timeout", "524", "connection_error").
        latency_ms: Observed latency in milliseconds, None if request failed.
    """

    healthy: bool
    status_code: int | None = None
    error: str | None = None
    latency_ms: float | None = None


class LBResponse(BaseModel):
    """Parsed response from an LB custom-path call."""

    status_code: int
    data: dict[str, Any]
    raw: bytes | None = None


def lb_endpoint_url(endpoint_id: str, path: str = "/") -> str:
    """Build a RunPod load-balancing serverless URL.

    >>> lb_endpoint_url("eptest00000000", "/embed")
    'https://eptest00000000.api.runpod.ai/embed'
    """
    endpoint_id = endpoint_id.strip().rstrip("/")
    path = path if path.startswith("/") else f"/{path}"
    return f"https://{endpoint_id}.api.runpod.ai{path}"


class LBClient:
    """Async httpx wrapper for RunPod load-balancing Serverless endpoints."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_s: float = 120.0,
        retry_delays: tuple[float, ...] = (1.0, 3.0, 9.0),
        max_retry_after_s: float = DEFAULT_MAX_RETRY_AFTER_DELAY_S,
        sleep: SleepFunc = asyncio.sleep,
        clock: ClockFunc = _utc_now,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._retry_delays = retry_delays
        self._max_retry_after_s = max_retry_after_s
        self._sleep = sleep
        self._clock = clock
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout_s,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            transport=self._transport,
        )

    async def _retry_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempts = len(self._retry_delays) + 1
        next_delay_s = 0.0
        last_error: Exception | None = None

        for attempt_index in range(attempts):
            if next_delay_s:
                await self._sleep(next_delay_s)

            async with self._client() as client:
                try:
                    response = await client.request(method, url, **kwargs)
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
        raise RuntimeError("LB request failed without response or exception")

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

    async def post(
        self,
        endpoint_id: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> LBResponse:
        """POST to a custom path on an LB endpoint."""
        url = lb_endpoint_url(endpoint_id, path)
        response = await self._retry_request("POST", url, json=json)
        return _parse_lb_response(response)

    async def get(
        self,
        endpoint_id: str,
        path: str,
    ) -> LBResponse:
        """GET from a custom path on an LB endpoint."""
        url = lb_endpoint_url(endpoint_id, path)
        response = await self._retry_request("GET", url)
        return _parse_lb_response(response)

    async def ping(self, endpoint_id: str) -> bool:
        """GET /ping — returns True if the LB endpoint reports healthy (200)."""
        url = lb_endpoint_url(endpoint_id, "/ping")
        async with self._client() as client:
            response = await client.get(url)
            return response.status_code == 200

    async def probe(
        self,
        endpoint_id: str,
        *,
        path: str = "/ping",
        timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    ) -> ProbeResult:
        """Probe an LB endpoint with a bounded timeout and structured result.

        Args:
            endpoint_id: The RunPod endpoint ID.
            path: The health check path (default /ping).
            timeout_s: Bounded timeout for the probe (default 5s).

        Returns:
            ProbeResult with healthy status, status_code, error, and latency_ms.
        """
        url = lb_endpoint_url(endpoint_id, path)
        start = time.monotonic()
        try:
            async with self._client() as client:
                response = await client.get(url, timeout=timeout_s)
                latency_ms = (time.monotonic() - start) * 1000
                return ProbeResult(
                    healthy=response.status_code == 200,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                )
        except httpx.TimeoutException:
            latency_ms = (time.monotonic() - start) * 1000
            return ProbeResult(
                healthy=False,
                error="timeout",
                latency_ms=latency_ms,
            )
        except httpx.HTTPStatusError as exc:
            latency_ms = (time.monotonic() - start) * 1000
            if exc.response.status_code == 524:
                return ProbeResult(
                    healthy=False,
                    status_code=524,
                    error="524",
                    latency_ms=latency_ms,
                )
            return ProbeResult(
                healthy=False,
                status_code=exc.response.status_code,
                latency_ms=latency_ms,
            )
        except httpx.HTTPError:
            latency_ms = (time.monotonic() - start) * 1000
            return ProbeResult(
                healthy=False,
                error="connection_error",
                latency_ms=latency_ms,
            )


def _status_error(response: httpx.Response, message: str) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        message=message,
        request=response.request,
        response=response,
    )


def _parse_lb_response(response: httpx.Response) -> LBResponse:
    try:
        data = response.json()
    except Exception:  # reason: non-JSON upstream body handled as empty payload
        data = {}
    return LBResponse(
        status_code=response.status_code,
        data=data if isinstance(data, dict) else {"value": data},
    )


__all__ = [
    "DEFAULT_MAX_RETRY_AFTER_DELAY_S",
    "DEFAULT_PROBE_TIMEOUT_S",
    "LBClient",
    "LBResponse",
    "ProbeResult",
    "lb_endpoint_url",
    "parse_retry_after",
]
