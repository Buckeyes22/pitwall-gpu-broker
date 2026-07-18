"""RunPod Serverless queue-based client.

Wraps the classic RunPod serverless surface:

    POST /v2/{ENDPOINT_ID}/runsync
    POST /v2/{ENDPOINT_ID}/run
    GET  /v2/{ENDPOINT_ID}/status/{JOB_ID}
    GET  /v2/{ENDPOINT_ID}/health
    POST /v2/{ENDPOINT_ID}/cancel/{JOB_ID}
    POST /v2/{ENDPOINT_ID}/purge-queue

Authorization: ``Bearer {RUNPOD_API_KEY}``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx
from pydantic import BaseModel

from pitwall.rate_limits.retry_after import (
    DEFAULT_MAX_RETRY_AFTER_DELAY_S,
    parse_retry_after,
)

log = logging.getLogger("pitwall.runpod_client.queue")

RUNPOD_API_BASE = "https://api.runpod.ai/v2"

SleepFunc = Callable[[float], Awaitable[object]]
ClockFunc = Callable[[], dt.datetime]
On429Func = Callable[["RateLimitFailure"], Awaitable[object]]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class QueueJob(BaseModel):
    """Parsed response from /runsync or /run."""

    id: str
    status: str
    output: dict[str, Any] | None = None
    error: str | None = None
    raw: dict[str, Any]


class QueueHealth(BaseModel):
    """Parsed response from /health."""

    jobs: dict[str, int]
    raw: dict[str, Any]


class QueueCancelResult(BaseModel):
    """Parsed response from /cancel/{job_id}."""

    cancelled: bool
    raw: dict[str, Any]


class QueuePurgeResult(BaseModel):
    """Parsed response from /purge-queue."""

    purged: int
    raw: dict[str, Any]


class RateLimitFailure(BaseModel):
    """Recorded 429 event with enough endpoint context to debug.

    Attributes:
        endpoint_id: RunPod endpoint ID that returned 429.
        path: HTTP path segment (e.g. ``/runsync``, ``/run``).
        retry_after_s: Bounded delay in seconds before next attempt.
        retry_after_header: Raw ``Retry-After`` header value (may be None).
        status_code: Always 429; included for structured logging parity.
        occurred_at: Wall-clock timestamp when the 429 was received.
    """

    endpoint_id: str
    path: str
    retry_after_s: float
    retry_after_header: str | None = None
    status_code: int = 429
    occurred_at: dt.datetime


def _endpoint_url(endpoint_id: str) -> str:
    return f"{RUNPOD_API_BASE}/{endpoint_id}"


class QueueClient:
    """Async httpx wrapper for RunPod queue-based Serverless endpoints."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_s: int = 600,
        retry_delays: tuple[float, ...] = (1.0, 3.0, 9.0),
        max_retry_after_s: float = DEFAULT_MAX_RETRY_AFTER_DELAY_S,
        sleep: SleepFunc = asyncio.sleep,
        clock: ClockFunc = _utc_now,
        transport: httpx.AsyncBaseTransport | None = None,
        on_429: On429Func | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._retry_delays = retry_delays
        self._max_retry_after_s = max_retry_after_s
        self._sleep = sleep
        self._clock = clock
        self._transport = transport
        self._on_429 = on_429

    def _client(self, base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            timeout=self._timeout_s,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            transport=self._transport,
        )

    async def _retry_post(
        self, base_url: str, path: str, *, json: dict[str, Any]
    ) -> dict[str, Any]:
        endpoint_id = _extract_endpoint_id(base_url)
        attempts = len(self._retry_delays) + 1
        next_delay_s = 0.0
        last_error: Exception | None = None

        for attempt_index in range(attempts):
            if next_delay_s:
                await self._sleep(next_delay_s)

            async with self._client(base_url) as client:
                try:
                    response = await client.post(path, json=json)
                except httpx.HTTPError as exc:
                    last_error = exc
                    next_delay_s = self._next_configured_delay(attempt_index)
                    continue

                if response.status_code == 429:
                    retry_after_header = response.headers.get("Retry-After")
                    next_delay_s = self._retry_after_delay(response, attempt_index)
                    last_error = _status_error(response, "Rate limited")
                    failure = RateLimitFailure(
                        endpoint_id=endpoint_id,
                        path=path,
                        retry_after_s=next_delay_s,
                        retry_after_header=retry_after_header,
                        occurred_at=self._clock(),
                    )
                    log.warning(
                        "RunPod 429 endpoint=%s path=%s retry_after=%.1fs",
                        endpoint_id,
                        path,
                        next_delay_s,
                    )
                    if self._on_429 is not None:
                        await self._on_429(failure)
                    continue

                if response.status_code < 500:
                    response.raise_for_status()
                    return cast(dict[str, Any], response.json())

                last_error = _status_error(response, f"Server error: {response.status_code}")
                next_delay_s = self._next_configured_delay(attempt_index)

        if last_error is not None:
            raise last_error
        raise RuntimeError("queue request failed without response or exception")

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

    async def runsync(
        self,
        endpoint_id: str,
        *,
        input: dict[str, Any],
        webhook: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> QueueJob:
        """POST /v2/{endpoint_id}/runsync — synchronous execution."""
        base = _endpoint_url(endpoint_id)
        payload: dict[str, Any] = {"input": input}
        if webhook is not None:
            payload["webhook"] = webhook
        if policy is not None:
            payload["policy"] = policy
        response = await self._retry_post(base, "/runsync", json=payload)
        return _parse_queue_job(response)

    async def run(
        self,
        endpoint_id: str,
        *,
        input: dict[str, Any],
        webhook: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> QueueJob:
        """POST /v2/{endpoint_id}/run — asynchronous execution."""
        base = _endpoint_url(endpoint_id)
        payload: dict[str, Any] = {"input": input}
        if webhook is not None:
            payload["webhook"] = webhook
        if policy is not None:
            payload["policy"] = policy
        response = await self._retry_post(base, "/run", json=payload)
        return _parse_queue_job(response)

    async def status(self, endpoint_id: str, job_id: str) -> QueueJob:
        """GET /v2/{endpoint_id}/status/{job_id}."""
        base = _endpoint_url(endpoint_id)
        async with self._client(base) as client:
            response = await client.get(f"/status/{job_id}")
            response.raise_for_status()
            data = response.json()
        return _parse_queue_job(data)

    async def health(self, endpoint_id: str) -> QueueHealth:
        """GET /v2/{endpoint_id}/health."""
        base = _endpoint_url(endpoint_id)
        async with self._client(base) as client:
            response = await client.get("/health")
            response.raise_for_status()
            data = response.json()
        jobs = {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
        return QueueHealth(jobs=jobs, raw=data)

    async def cancel(self, endpoint_id: str, job_id: str) -> QueueCancelResult:
        """POST /v2/{endpoint_id}/cancel/{job_id}."""
        base = _endpoint_url(endpoint_id)
        async with self._client(base) as client:
            response = await client.post(f"/cancel/{job_id}")
            response.raise_for_status()
            data = response.json()
        return QueueCancelResult(
            cancelled=bool(data.get("cancelled", False)),
            raw=dict(data),
        )

    async def purge_queue(self, endpoint_id: str) -> QueuePurgeResult:
        """POST /v2/{endpoint_id}/purge-queue."""
        base = _endpoint_url(endpoint_id)
        async with self._client(base) as client:
            response = await client.post("/purge-queue")
            response.raise_for_status()
            data = response.json()
        return QueuePurgeResult(
            purged=int(data.get("purged", 0)),
            raw=dict(data),
        )


def _extract_endpoint_id(base_url: str) -> str:
    """Extract the endpoint ID from a base URL like ``https://api.runpod.ai/v2/abc123``."""
    stripped = base_url.rstrip("/")
    return stripped.rsplit("/", maxsplit=1)[-1]


def _status_error(response: httpx.Response, message: str) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        message=message,
        request=response.request,
        response=response,
    )


def _parse_queue_job(data: dict[str, Any]) -> QueueJob:
    return QueueJob(
        id=str(data.get("id", "")),
        status=str(data.get("status", "")),
        output=data.get("output"),
        error=data.get("error"),
        raw=data,
    )


def queue_url(endpoint_id: str, path: str = "") -> str:
    """Build a RunPod queue-based serverless URL.

    >>> queue_url("abc123", "/runsync")
    'https://api.runpod.ai/v2/abc123/runsync'
    """
    base = _endpoint_url(endpoint_id)
    if path:
        return f"{base}{path}"
    return base


__all__ = [
    "DEFAULT_MAX_RETRY_AFTER_DELAY_S",
    "RUNPOD_API_BASE",
    "On429Func",
    "QueueCancelResult",
    "QueueClient",
    "QueueHealth",
    "QueueJob",
    "QueuePurgeResult",
    "RateLimitFailure",
    "parse_retry_after",
    "queue_url",
]
