"""Property tests for hedged provider fan-out bounds."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pitwall.routing.hedging import HedgedProviderError, HedgedProviderRequest, race_providers

pytestmark = pytest.mark.property


@dataclass(frozen=True, slots=True)
class FakeProvider:
    id: str


@dataclass(slots=True)
class AttemptLog:
    started: list[str] = field(default_factory=list)


AttemptCallable = Callable[[FakeProvider], Awaitable[str]]


def _always_failing(log: AttemptLog) -> AttemptCallable:
    async def call(provider: FakeProvider) -> str:
        log.started.append(provider.id)
        await asyncio.sleep(0)
        raise RuntimeError(f"{provider.id} failed")

    return call


@settings(max_examples=30)
@given(
    provider_count=st.integers(min_value=1, max_value=8),
    max_attempts=st.integers(min_value=1, max_value=8),
    max_concurrency=st.integers(min_value=1, max_value=8),
)
@pytest.mark.anyio
async def test_started_attempts_never_exceed_bounded_fanout(
    provider_count: int,
    max_attempts: int,
    max_concurrency: int,
) -> None:
    providers = [FakeProvider(id=f"prov_{index}") for index in range(provider_count)]
    log = AttemptLog()

    with pytest.raises(HedgedProviderError) as exc_info:
        await race_providers(
            HedgedProviderRequest(
                providers=providers,
                call_provider=_always_failing(log),
                hedge_delay_s=0,
                max_attempts=max_attempts,
                max_concurrency=max_concurrency,
            )
        )

    expected_bound = min(provider_count, max_attempts)
    assert len(log.started) == expected_bound
    assert len(exc_info.value.attempted_provider_ids) == expected_bound
    assert len(set(log.started)) == len(log.started)
