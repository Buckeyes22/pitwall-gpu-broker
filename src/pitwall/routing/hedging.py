"""Async hedged provider racing.

Latency hedging is for request paths that already resolved an ordered provider
chain and want bounded parallelism: start the primary, wait a short delay, then
start backup attempts only if no provider has succeeded yet. The first success
wins and all other in-flight attempts are cancelled.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_HEDGE_DELAY_S = 0.050
DEFAULT_HEDGED_MAX_ATTEMPTS = 2
DEFAULT_HEDGED_MAX_CONCURRENCY = 2

type ProviderCallable[ProviderT, ResultT] = Callable[[ProviderT], Awaitable[ResultT]]
type ProviderIdCallable[ProviderT] = Callable[[ProviderT], str]


def _default_provider_id(provider: object) -> str:
    value = provider.get("id") if isinstance(provider, Mapping) else getattr(provider, "id", None)

    if not isinstance(value, str) or not value:
        raise ValueError("provider must include a non-empty id")
    return value


@dataclass(frozen=True, slots=True)
class HedgedProviderRequest[ProviderT, ResultT]:
    """Inputs for one bounded provider race."""

    providers: Sequence[ProviderT]
    call_provider: ProviderCallable[ProviderT, ResultT]
    hedge_delay_s: float = DEFAULT_HEDGE_DELAY_S
    max_attempts: int = DEFAULT_HEDGED_MAX_ATTEMPTS
    max_concurrency: int = DEFAULT_HEDGED_MAX_CONCURRENCY
    provider_id: ProviderIdCallable[ProviderT] = _default_provider_id


@dataclass(frozen=True, slots=True)
class HedgedProviderResult[ProviderT, ResultT]:
    """Winning provider result and the attempted provider chain."""

    value: ResultT
    provider: ProviderT
    provider_id: str
    attempted_provider_ids: tuple[str, ...]
    elapsed_s: float

    @property
    def hedged(self) -> bool:
        return len(self.attempted_provider_ids) > 1

    @property
    def attempt_count(self) -> int:
        return len(self.attempted_provider_ids)


class HedgedProviderError(RuntimeError):
    """Raised when every attempted provider fails before any success."""

    def __init__(
        self,
        message: str,
        *,
        attempted_provider_ids: Sequence[str],
        cause: BaseException | None = None,
        attempted_errors: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.attempted_provider_ids = tuple(attempted_provider_ids)
        self.cause = cause
        self.attempted_errors = dict(attempted_errors or {})


@dataclass(frozen=True, slots=True)
class _ActiveAttempt[ProviderT]:
    index: int
    provider: ProviderT
    provider_id: str


async def race_providers[ProviderT, ResultT](
    request: HedgedProviderRequest[ProviderT, ResultT],
) -> HedgedProviderResult[ProviderT, ResultT]:
    """Race an ordered provider chain with bounded hedged fan-out.

    The primary provider starts immediately. Backup providers start after
    ``hedge_delay_s`` if no attempt has succeeded, or immediately when the
    current active set has failed and there are still providers left. At most
    ``max_attempts`` total providers are started, and at most ``max_concurrency``
    provider calls run concurrently.
    """

    selected_providers, selected_provider_ids, max_concurrency = _validate_request(request)
    started_at = time.perf_counter()
    attempted_provider_ids: list[str] = []
    attempted_errors: dict[str, str] = {}
    active: dict[asyncio.Future[ResultT], _ActiveAttempt[ProviderT]] = {}
    completed: list[asyncio.Future[ResultT]] = []
    next_index = 0
    hedge_open = False
    last_error: BaseException | None = None

    def remember_completion(done: asyncio.Future[ResultT]) -> None:
        completed.append(done)

    def start_next() -> None:
        nonlocal next_index
        if next_index >= len(selected_providers):
            return
        provider = selected_providers[next_index]
        provider_id = selected_provider_ids[next_index]
        task: asyncio.Future[ResultT] = asyncio.ensure_future(request.call_provider(provider))
        task.add_done_callback(remember_completion)
        active[task] = _ActiveAttempt(
            index=next_index,
            provider=provider,
            provider_id=provider_id,
        )
        attempted_provider_ids.append(provider_id)
        next_index += 1

    def fill_open_slots() -> None:
        while len(active) < max_concurrency and next_index < len(selected_providers):
            start_next()

    start_next()
    try:
        while active:
            timeout_s: float | None = None
            if not hedge_open and next_index < len(selected_providers):
                elapsed_s = time.perf_counter() - started_at
                timeout_s = max(0.0, request.hedge_delay_s - elapsed_s)

            done, _pending = await asyncio.wait(
                set(active),
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                hedge_open = True
                fill_open_slots()
                continue

            done_tasks = set(done)
            ordered_done = [task for task in completed if task in done_tasks]
            completed[:] = [task for task in completed if task not in done_tasks]

            for task in ordered_done:
                attempt = active.pop(task)
                if task.cancelled():
                    attempt_error = asyncio.CancelledError(
                        f"provider {attempt.provider_id} attempt was cancelled"
                    )
                    last_error = attempt_error
                    attempted_errors[attempt.provider_id] = str(attempt_error)
                    continue

                task_error = task.exception()
                if task_error is not None:
                    last_error = task_error
                    attempted_errors[attempt.provider_id] = str(task_error)
                    continue

                value = task.result()
                await _cancel_active(active)
                return HedgedProviderResult(
                    value=value,
                    provider=attempt.provider,
                    provider_id=attempt.provider_id,
                    attempted_provider_ids=tuple(attempted_provider_ids),
                    elapsed_s=time.perf_counter() - started_at,
                )

            if not active and next_index < len(selected_providers):
                start_next()
            elif hedge_open and next_index < len(selected_providers):
                fill_open_slots()
    except asyncio.CancelledError:
        await _cancel_active(active)
        raise

    race_error = HedgedProviderError(
        "hedged provider race failed before any provider succeeded",
        attempted_provider_ids=attempted_provider_ids,
        cause=last_error,
        attempted_errors=attempted_errors,
    )
    if last_error is not None:
        raise race_error from last_error
    raise race_error


def _validate_request[ProviderT, ResultT](
    request: HedgedProviderRequest[ProviderT, ResultT],
) -> tuple[tuple[ProviderT, ...], tuple[str, ...], int]:
    if not request.providers:
        raise ValueError("providers must contain at least one provider")
    _validate_non_negative_finite(request.hedge_delay_s, field_name="hedge_delay_s")
    _validate_positive_int(request.max_attempts, field_name="max_attempts")
    _validate_positive_int(request.max_concurrency, field_name="max_concurrency")

    max_attempts = min(request.max_attempts, len(request.providers))
    max_concurrency = min(request.max_concurrency, max_attempts)
    selected = tuple(request.providers[:max_attempts])
    provider_ids = tuple(request.provider_id(provider) for provider in selected)
    _validate_provider_ids(provider_ids)
    return selected, provider_ids, max_concurrency


async def _cancel_active(active: Mapping[asyncio.Future[Any], _ActiveAttempt[Any]]) -> None:
    if not active:
        return
    tasks = tuple(active)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _validate_non_negative_finite(value: float, *, field_name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")


def _validate_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _validate_provider_ids(provider_ids: tuple[str, ...]) -> None:
    seen: set[str] = set()
    for provider_id in provider_ids:
        if not provider_id:
            raise ValueError("provider ids must be non-empty strings")
        if provider_id in seen:
            raise ValueError(f"provider id {provider_id!r} is duplicated")
        seen.add(provider_id)


__all__ = [
    "DEFAULT_HEDGE_DELAY_S",
    "DEFAULT_HEDGED_MAX_ATTEMPTS",
    "DEFAULT_HEDGED_MAX_CONCURRENCY",
    "HedgedProviderError",
    "HedgedProviderRequest",
    "HedgedProviderResult",
    "race_providers",
]
