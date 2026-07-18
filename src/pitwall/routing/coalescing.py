"""Async request coalescing for duplicate in-flight work."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _InflightExecution[T]:
    task: asyncio.Task[T]
    waiters: int = 0


class AsyncRequestCoalescer[T]:
    """Fan out concurrent calls with the same key from one shared execution."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._inflight: dict[str, _InflightExecution[T]] = {}

    @property
    def inflight_count(self) -> int:
        """Return the current number of tracked in-flight keys."""
        return len(self._inflight)

    async def run(self, key: str, execute: Callable[[], Awaitable[T]]) -> T:
        """Run or join the in-flight execution for ``key``.

        The first caller for a key starts an internal execution task. Concurrent
        callers await that task and receive the same result or exception. Caller
        cancellation only detaches that caller; shared work is cancelled once no
        waiters remain.
        """
        if key == "":
            raise ValueError("coalescing key must be non-empty")

        async with self._lock:
            inflight = self._inflight.get(key)
            if inflight is None:
                task = asyncio.create_task(_run_execute(execute))
                task.add_done_callback(_consume_task_result)
                inflight = _InflightExecution(task=task)
                self._inflight[key] = inflight
            inflight.waiters += 1
        try:
            return await asyncio.shield(inflight.task)
        finally:
            await self._release_waiter(key, inflight)

    async def _release_waiter(self, key: str, inflight: _InflightExecution[T]) -> None:
        async with self._lock:
            if self._inflight.get(key) is not inflight:
                return
            inflight.waiters -= 1
            if inflight.task.done():
                del self._inflight[key]
                return
            if inflight.waiters == 0:
                inflight.task.cancel()
                del self._inflight[key]


async def _run_execute[T](execute: Callable[[], Awaitable[T]]) -> T:
    return await execute()


def _consume_task_result[T](task: asyncio.Task[T]) -> None:
    if not task.cancelled():
        task.exception()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_payload_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_inference_coalescing_key(
    *,
    idempotency_key: str | None,
    capability_id: str,
    provider_id: str,
    capability_params: Mapping[str, Any],
) -> str:
    """Build a stable key for sync inference request coalescing.

    Requests with client idempotency keys are scoped by the key hash and the
    canonical request content. Anonymous requests coalesce only by content.
    Different idempotency keys never share work, even when payloads match.
    """
    content_digest = _canonical_payload_digest(
        {
            "capability_id": capability_id,
            "provider_id": provider_id,
            "capability_params": capability_params,
        }
    )
    if idempotency_key is None:
        return f"content:{content_digest}"
    return f"idempotency:{_sha256_text(idempotency_key)}:{content_digest}"


__all__ = [
    "AsyncRequestCoalescer",
    "build_inference_coalescing_key",
]
