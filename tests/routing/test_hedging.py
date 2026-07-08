"""Hermetic async tests for latency hedged provider racing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pytest

from pitwall.routing.hedging import (
    HedgedProviderError,
    HedgedProviderRequest,
    HedgedProviderResult,
    race_providers,
)


@dataclass(frozen=True, slots=True)
class FakeProvider:
    id: str
    name: str


@dataclass(slots=True)
class AttemptLog:
    started: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)


AttemptCallable = Callable[[FakeProvider], Awaitable[str]]


def _provider(provider_id: str) -> FakeProvider:
    return FakeProvider(id=provider_id, name=provider_id)


def _successful_caller(
    *,
    delays: dict[str, float],
    log: AttemptLog,
) -> AttemptCallable:
    async def call(provider: FakeProvider) -> str:
        log.started.append(provider.id)
        try:
            await asyncio.sleep(delays[provider.id])
        except asyncio.CancelledError:
            log.cancelled.append(provider.id)
            raise
        log.completed.append(provider.id)
        return f"ok:{provider.id}"

    return call


def _failing_caller(
    *,
    delays: dict[str, float],
    failures: dict[str, Exception],
    log: AttemptLog,
) -> AttemptCallable:
    async def call(provider: FakeProvider) -> str:
        log.started.append(provider.id)
        try:
            await asyncio.sleep(delays[provider.id])
        except asyncio.CancelledError:
            log.cancelled.append(provider.id)
            raise
        log.completed.append(provider.id)
        failure = failures.get(provider.id)
        if failure is not None:
            raise failure
        return f"ok:{provider.id}"

    return call


@pytest.mark.anyio
async def test_primary_success_before_delay_does_not_start_backup() -> None:
    providers = [_provider("primary"), _provider("backup")]
    log = AttemptLog()

    result = await race_providers(
        HedgedProviderRequest(
            providers=providers,
            call_provider=_successful_caller(
                delays={"primary": 0.001, "backup": 0.050},
                log=log,
            ),
            hedge_delay_s=0.020,
        )
    )

    assert isinstance(result, HedgedProviderResult)
    assert result.value == "ok:primary"
    assert result.provider_id == "primary"
    assert result.attempted_provider_ids == ("primary",)
    assert result.hedged is False
    assert log.started == ["primary"]
    assert log.cancelled == []


@pytest.mark.anyio
async def test_backup_starts_after_delay_and_wins_when_primary_is_slow() -> None:
    providers = [_provider("primary"), _provider("backup")]
    log = AttemptLog()

    result = await race_providers(
        HedgedProviderRequest(
            providers=providers,
            call_provider=_successful_caller(
                delays={"primary": 0.100, "backup": 0.001},
                log=log,
            ),
            hedge_delay_s=0.005,
        )
    )

    assert result.value == "ok:backup"
    assert result.provider_id == "backup"
    assert result.attempted_provider_ids == ("primary", "backup")
    assert result.hedged is True
    assert "primary" in log.cancelled


@pytest.mark.anyio
async def test_primary_failure_before_delay_starts_backup_immediately() -> None:
    providers = [_provider("primary"), _provider("backup")]
    log = AttemptLog()

    result = await race_providers(
        HedgedProviderRequest(
            providers=providers,
            call_provider=_failing_caller(
                delays={"primary": 0.001, "backup": 0.001},
                failures={"primary": RuntimeError("boom")},
                log=log,
            ),
            hedge_delay_s=0.050,
        )
    )

    assert result.value == "ok:backup"
    assert result.provider_id == "backup"
    assert result.attempted_provider_ids == ("primary", "backup")
    assert result.hedged is True
    assert log.completed == ["primary", "backup"]


@pytest.mark.anyio
async def test_success_cancels_later_started_attempts() -> None:
    providers = [_provider("primary"), _provider("backup"), _provider("third")]
    log = AttemptLog()

    result = await race_providers(
        HedgedProviderRequest(
            providers=providers,
            call_provider=_successful_caller(
                delays={"primary": 0.100, "backup": 0.001, "third": 0.100},
                log=log,
            ),
            hedge_delay_s=0.001,
            max_attempts=3,
            max_concurrency=3,
        )
    )

    assert result.value == "ok:backup"
    assert result.provider_id == "backup"
    assert set(result.attempted_provider_ids) == {"primary", "backup", "third"}
    assert "primary" in log.cancelled
    assert "third" in log.cancelled


@pytest.mark.anyio
async def test_all_failures_raise_with_attempt_metadata() -> None:
    providers = [_provider("primary"), _provider("backup")]
    log = AttemptLog()

    with pytest.raises(HedgedProviderError) as exc_info:
        await race_providers(
            HedgedProviderRequest(
                providers=providers,
                call_provider=_failing_caller(
                    delays={"primary": 0.001, "backup": 0.001},
                    failures={
                        "primary": RuntimeError("primary failed"),
                        "backup": ValueError("backup failed"),
                    },
                    log=log,
                ),
                hedge_delay_s=0.001,
            )
        )

    assert exc_info.value.attempted_provider_ids == ("primary", "backup")
    assert exc_info.value.attempted_errors == {
        "primary": "primary failed",
        "backup": "backup failed",
    }
    assert isinstance(exc_info.value.cause, ValueError)


@pytest.mark.anyio
async def test_max_attempts_caps_provider_fanout() -> None:
    providers = [_provider("primary"), _provider("backup"), _provider("third")]
    log = AttemptLog()

    with pytest.raises(HedgedProviderError) as exc_info:
        await race_providers(
            HedgedProviderRequest(
                providers=providers,
                call_provider=_failing_caller(
                    delays={"primary": 0.001, "backup": 0.001, "third": 0.001},
                    failures={
                        "primary": RuntimeError("primary failed"),
                        "backup": RuntimeError("backup failed"),
                        "third": RuntimeError("third failed"),
                    },
                    log=log,
                ),
                hedge_delay_s=0.001,
                max_attempts=2,
                max_concurrency=3,
            )
        )

    assert exc_info.value.attempted_provider_ids == ("primary", "backup")
    assert log.started == ["primary", "backup"]


@pytest.mark.anyio
async def test_max_concurrency_caps_parallel_started_attempts() -> None:
    providers = [_provider("primary"), _provider("backup"), _provider("third")]
    log = AttemptLog()

    result = await race_providers(
        HedgedProviderRequest(
            providers=providers,
            call_provider=_failing_caller(
                delays={"primary": 0.020, "backup": 0.020, "third": 0.001},
                failures={
                    "primary": RuntimeError("primary failed"),
                    "backup": RuntimeError("backup failed"),
                },
                log=log,
            ),
            hedge_delay_s=0.001,
            max_attempts=3,
            max_concurrency=2,
        )
    )

    assert result.value == "ok:third"
    assert result.provider_id == "third"
    assert result.attempted_provider_ids == ("primary", "backup", "third")
    assert log.started[:2] == ["primary", "backup"]
    assert log.completed == ["primary", "backup", "third"]


@pytest.mark.anyio
async def test_validation_rejects_invalid_options() -> None:
    providers = [_provider("primary")]
    call_provider = _successful_caller(delays={"primary": 0.001}, log=AttemptLog())

    invalid_requests = [
        HedgedProviderRequest(providers=[], call_provider=call_provider),
        HedgedProviderRequest(providers=providers, call_provider=call_provider, hedge_delay_s=-0.1),
        HedgedProviderRequest(providers=providers, call_provider=call_provider, max_attempts=0),
        HedgedProviderRequest(providers=providers, call_provider=call_provider, max_concurrency=0),
    ]

    for request in invalid_requests:
        with pytest.raises(ValueError):
            await race_providers(request)
