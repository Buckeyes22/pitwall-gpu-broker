"""Shared OpenAI-compatible fallback execution.

The executor stops at upstream response headers.  The caller owns the returned
response stream and must close both that response and the HTTP client after the
body is relayed.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

import httpx

from pitwall.core.models import Provider
from pitwall.routing.openai import (
    MAX_OPENAI_ATTEMPTS,
    build_openai_url,
    openai_base_url_for_provider,
)

DEFAULT_OPENAI_FALLBACK_BUDGET_S = 5.0


@dataclass(frozen=True, slots=True)
class OpenAIProxyRequest:
    """Request data reused for every provider attempt."""

    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes
    client: httpx.AsyncClient
    fallback_budget_s: float = DEFAULT_OPENAI_FALLBACK_BUDGET_S
    max_attempts: int = MAX_OPENAI_ATTEMPTS


@dataclass(frozen=True, slots=True)
class OpenAIProxyResult:
    """Selected upstream response and the providers actually attempted."""

    response: httpx.Response
    provider: Provider
    attempted_provider_ids: tuple[str, ...]
    elapsed_s: float

    @property
    def upstream_response(self) -> httpx.Response:
        return self.response

    @property
    def provider_id(self) -> str:
        return self.provider.id

    @property
    def fallback_chain(self) -> tuple[str, ...]:
        return self.attempted_provider_ids

    @property
    def attempt_count(self) -> int:
        return len(self.attempted_provider_ids)


class OpenAIProxyExecutionError(RuntimeError):
    """Raised when no provider returns response headers."""

    def __init__(
        self,
        message: str,
        *,
        attempted_provider_ids: Sequence[str],
        cause: BaseException | None = None,
        attempted_errors: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.attempted_provider_ids = tuple(attempted_provider_ids)
        self.cause = cause
        self.attempted_errors = attempted_errors or {}


async def execute_openai_with_fallback(
    request_ctx: OpenAIProxyRequest,
    providers: list[Provider],
    *,
    on_attempt: (Callable[[tuple[str, ...]], None] | Callable[[tuple[str, ...]], Awaitable[None]])
    | None = None,
) -> OpenAIProxyResult:
    """Execute an OpenAI-compatible request across a bounded provider chain.

    A provider is retried only when it fails before response body relay begins:
    either an upstream 5xx arrives, or transport fails before response headers
    arrive.  Responses below 500, including 4xx, are returned immediately.

    If ``on_attempt`` is provided, it will be called after each provider attempt
    with the current tuple of attempted provider IDs. This can be used to persist
    the fallback chain to a database after each attempt.
    """

    if isinstance(request_ctx.max_attempts, bool) or request_ctx.max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    if request_ctx.fallback_budget_s <= 0:
        raise ValueError("fallback_budget_s must be positive")

    started_at = time.perf_counter()
    deadline = started_at + request_ctx.fallback_budget_s
    eligible_providers = _providers_with_openai_urls(
        providers,
        max_attempts=min(request_ctx.max_attempts, MAX_OPENAI_ATTEMPTS),
    )
    attempted_provider_ids: list[str] = []
    attempted_errors: dict[str, str] = {}
    last_error: BaseException | None = None

    for index, provider in enumerate(eligible_providers):
        remaining_s = deadline - time.perf_counter()
        if remaining_s <= 0:
            break

        attempted_provider_ids.append(provider.id)
        if on_attempt is not None:
            if inspect.iscoroutinefunction(on_attempt):
                await on_attempt(tuple(attempted_provider_ids))
            else:
                on_attempt(tuple(attempted_provider_ids))
        try:
            response = await _send_until_headers(
                request_ctx,
                provider,
                timeout_s=remaining_s,
            )
        except (TimeoutError, httpx.HTTPError) as exc:
            last_error = exc
            attempted_errors[provider.id] = str(exc)
            if index < len(eligible_providers) - 1 and deadline > time.perf_counter():
                continue
            break

        if not _is_retryable_response(response):
            return OpenAIProxyResult(
                response=response,
                provider=provider,
                attempted_provider_ids=tuple(attempted_provider_ids),
                elapsed_s=time.perf_counter() - started_at,
            )

        attempted_errors[provider.id] = f"HTTP {response.status_code}"
        if index >= len(eligible_providers) - 1 or deadline <= time.perf_counter():
            return OpenAIProxyResult(
                response=response,
                provider=provider,
                attempted_provider_ids=tuple(attempted_provider_ids),
                elapsed_s=time.perf_counter() - started_at,
            )

        await response.aclose()

    message = "openai proxy upstream request failed before response headers"
    error = OpenAIProxyExecutionError(
        message,
        attempted_provider_ids=attempted_provider_ids,
        cause=last_error,
        attempted_errors=attempted_errors,
    )
    if last_error is not None:
        raise error from last_error
    raise error


def _providers_with_openai_urls(
    providers: Sequence[Provider],
    *,
    max_attempts: int,
) -> tuple[Provider, ...]:
    eligible: list[Provider] = []
    for provider in providers:
        if len(eligible) >= max_attempts:
            break
        if openai_base_url_for_provider(provider) is None:
            continue
        eligible.append(provider)
    return tuple(eligible)


async def _send_until_headers(
    request_ctx: OpenAIProxyRequest,
    provider: Provider,
    *,
    timeout_s: float,
) -> httpx.Response:
    base_url = openai_base_url_for_provider(provider)
    if base_url is None:
        raise ValueError(f"provider {provider.id!r} does not have an OpenAI base URL")

    request = request_ctx.client.build_request(
        method=request_ctx.method,
        url=build_openai_url(base_url, request_ctx.path),
        headers=dict(request_ctx.headers),
        content=request_ctx.body,
    )
    async with asyncio.timeout(timeout_s):
        return await request_ctx.client.send(request, stream=True)


def _is_retryable_response(response: httpx.Response) -> bool:
    return 500 <= response.status_code < 600


__all__ = [
    "DEFAULT_OPENAI_FALLBACK_BUDGET_S",
    "OpenAIProxyExecutionError",
    "OpenAIProxyRequest",
    "OpenAIProxyResult",
    "execute_openai_with_fallback",
]
