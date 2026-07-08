"""Hermetic tests for budget-aware semantic result caching."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from pitwall.routing.semantic_cache import (
    BudgetAwareSemanticCache,
    CanonicalSemanticHasher,
    SemanticCachePolicy,
    SemanticCacheRunResult,
    build_semantic_cache_key,
)

pytestmark = pytest.mark.anyio

_BASE_TIME = dt.datetime(2026, 6, 2, 12, 0, 0, tzinfo=dt.UTC)


class _ManualClock:
    def __init__(self) -> None:
        self.now = _BASE_TIME

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, delta: dt.timedelta) -> None:
        self.now += delta


class _ConstantHasher:
    def __init__(self, signature: str) -> None:
        self.signature_value = signature
        self.calls: list[Mapping[str, Any]] = []

    def signature(
        self,
        *,
        capability_id: str,
        provider_id: str,
        capability_params: Mapping[str, Any],
    ) -> str:
        self.calls.append(
            {
                "capability_id": capability_id,
                "provider_id": provider_id,
                "capability_params": capability_params,
            }
        )
        return self.signature_value


def _policy(**overrides: object) -> SemanticCachePolicy:
    defaults: dict[str, object] = {
        "default_ttl": dt.timedelta(seconds=30),
        "max_entries": 2,
        "min_cache_estimate_usd": Decimal("0.010000"),
        "high_value_estimate_usd": Decimal("1.000000"),
        "high_value_ttl_multiplier": 3,
    }
    defaults.update(overrides)
    return SemanticCachePolicy(**defaults)


async def test_cache_hit_returns_cached_result_without_execute_or_spend() -> None:
    clock = _ManualClock()
    cache = BudgetAwareSemanticCache[dict[str, object]](
        policy=_policy(),
        now=clock,
    )
    spend_calls = 0

    async def execute() -> dict[str, object]:
        nonlocal spend_calls
        spend_calls += 1
        return {"embedding": [1, 2, 3]}

    first = await cache.run(
        capability_id="cap_embed",
        provider_id="prov_a",
        capability_params={"text": "  hello   world  "},
        estimated_cost_usd=Decimal("0.250000"),
        execute=execute,
    )
    second = await cache.run(
        capability_id="cap_embed",
        provider_id="prov_a",
        capability_params={"text": "hello world"},
        estimated_cost_usd=Decimal("0.250000"),
        execute=execute,
    )

    assert first == SemanticCacheRunResult(
        value={"embedding": [1, 2, 3]},
        hit=False,
        key=second.key,
    )
    assert second == SemanticCacheRunResult(
        value={"embedding": [1, 2, 3]},
        hit=True,
        key=first.key,
    )
    assert spend_calls == 1
    assert cache.entry_count == 1


async def test_miss_executes_and_caches_expensive_result() -> None:
    cache = BudgetAwareSemanticCache[str](policy=_policy())
    calls = 0

    async def execute() -> str:
        nonlocal calls
        calls += 1
        return "fresh"

    outcome = await cache.run(
        capability_id="cap_chat",
        provider_id="prov_a",
        capability_params={"prompt": "explain cache misses"},
        estimated_cost_usd=Decimal("0.125000"),
        execute=execute,
    )

    assert outcome.value == "fresh"
    assert outcome.hit is False
    assert calls == 1
    assert cache.entry_count == 1


async def test_concurrent_identical_cache_misses_share_one_budgeted_execution() -> None:
    cache = BudgetAwareSemanticCache[str](policy=_policy())
    calls = 0
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def execute() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
            return "shared-result"
        return "duplicate-spend"

    first_task = asyncio.create_task(
        cache.run(
            capability_id="cap_chat",
            provider_id="prov_a",
            capability_params={"prompt": "coalesce same semantic work"},
            estimated_cost_usd=Decimal("0.250000"),
            execute=execute,
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second_task = asyncio.create_task(
        cache.run(
            capability_id="cap_chat",
            provider_id="prov_a",
            capability_params={"prompt": "coalesce same semantic work"},
            estimated_cost_usd=Decimal("0.250000"),
            execute=execute,
        )
    )
    await asyncio.sleep(0)
    release_first.set()

    first, second = await asyncio.gather(first_task, second_task)

    assert first.value == "shared-result"
    assert second.value == "shared-result"
    assert first.hit is False
    assert second.hit is False
    assert first.key == second.key
    assert calls == 1
    assert cache.entry_count == 1


async def test_expired_entry_misses_and_refreshes() -> None:
    clock = _ManualClock()
    cache = BudgetAwareSemanticCache[int](
        policy=_policy(default_ttl=dt.timedelta(seconds=5)),
        now=clock,
    )
    calls = 0

    async def execute() -> int:
        nonlocal calls
        calls += 1
        return calls

    first = await cache.run(
        capability_id="cap",
        provider_id="prov",
        capability_params={"text": "ttl"},
        estimated_cost_usd=Decimal("0.050000"),
        execute=execute,
    )
    clock.advance(dt.timedelta(seconds=6))
    second = await cache.run(
        capability_id="cap",
        provider_id="prov",
        capability_params={"text": "ttl"},
        estimated_cost_usd=Decimal("0.050000"),
        execute=execute,
    )

    assert first.value == 1
    assert second.value == 2
    assert second.hit is False
    assert calls == 2
    assert cache.entry_count == 1


async def test_size_eviction_discards_cheapest_entry_first() -> None:
    cache = BudgetAwareSemanticCache[str](
        policy=_policy(max_entries=2),
    )

    async def execute(value: str) -> str:
        return value

    await cache.run(
        capability_id="cap",
        provider_id="prov",
        capability_params={"text": "cheap"},
        estimated_cost_usd=Decimal("0.020000"),
        execute=lambda: execute("cheap"),
    )
    expensive = await cache.run(
        capability_id="cap",
        provider_id="prov",
        capability_params={"text": "expensive"},
        estimated_cost_usd=Decimal("2.000000"),
        execute=lambda: execute("expensive"),
    )
    await cache.run(
        capability_id="cap",
        provider_id="prov",
        capability_params={"text": "middle"},
        estimated_cost_usd=Decimal("0.500000"),
        execute=lambda: execute("middle"),
    )
    cheap_again = await cache.run(
        capability_id="cap",
        provider_id="prov",
        capability_params={"text": "cheap"},
        estimated_cost_usd=Decimal("0.020000"),
        execute=lambda: execute("cheap-refreshed"),
    )
    expensive_again = await cache.run(
        capability_id="cap",
        provider_id="prov",
        capability_params={"text": "expensive"},
        estimated_cost_usd=Decimal("2.000000"),
        execute=lambda: execute("expensive-refreshed"),
    )

    assert cheap_again.value == "cheap-refreshed"
    assert cheap_again.hit is False
    assert expensive_again.value == expensive.value
    assert expensive_again.hit is True
    assert cache.entry_count == 2


async def test_cheap_results_are_not_cached() -> None:
    cache = BudgetAwareSemanticCache[int](
        policy=_policy(min_cache_estimate_usd=Decimal("0.100000")),
    )
    calls = 0

    async def execute() -> int:
        nonlocal calls
        calls += 1
        return calls

    first = await cache.run(
        capability_id="cap",
        provider_id="prov",
        capability_params={"text": "cheap"},
        estimated_cost_usd=Decimal("0.050000"),
        execute=execute,
    )
    second = await cache.run(
        capability_id="cap",
        provider_id="prov",
        capability_params={"text": "cheap"},
        estimated_cost_usd=Decimal("0.050000"),
        execute=execute,
    )

    assert first.value == 1
    assert second.value == 2
    assert first.hit is False
    assert second.hit is False
    assert calls == 2
    assert cache.entry_count == 0


def test_default_hasher_normalizes_prompt_whitespace_and_hides_content() -> None:
    first = build_semantic_cache_key(
        capability_id="cap",
        provider_id="prov",
        capability_params={"prompt": "  summarize\n\nthis\ttext "},
    )
    second = build_semantic_cache_key(
        capability_id="cap",
        provider_id="prov",
        capability_params={"prompt": "summarize this text"},
    )

    assert first == second
    assert "summarize" not in first
    assert "this text" not in first


async def test_pluggable_hasher_controls_signature() -> None:
    hasher = _ConstantHasher("embedding-hash-123")
    cache = BudgetAwareSemanticCache[str](
        policy=_policy(),
        hasher=hasher,
    )
    calls = 0

    async def execute() -> str:
        nonlocal calls
        calls += 1
        return f"value-{calls}"

    first = await cache.run(
        capability_id="cap",
        provider_id="prov",
        capability_params={"text": "first semantic twin"},
        estimated_cost_usd=Decimal("0.250000"),
        execute=execute,
    )
    second = await cache.run(
        capability_id="cap",
        provider_id="prov",
        capability_params={"text": "second semantic twin"},
        estimated_cost_usd=Decimal("0.250000"),
        execute=execute,
    )

    assert first.value == "value-1"
    assert second.value == "value-1"
    assert second.hit is True
    assert calls == 1
    assert len(hasher.calls) == 2


async def test_failed_execution_is_not_cached() -> None:
    class UpstreamFailure(Exception):
        pass

    cache = BudgetAwareSemanticCache[str](policy=_policy())
    calls = 0

    async def execute() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise UpstreamFailure("runpod failed")
        return "ok"

    with pytest.raises(UpstreamFailure, match="runpod failed"):
        await cache.run(
            capability_id="cap",
            provider_id="prov",
            capability_params={"text": "retry"},
            estimated_cost_usd=Decimal("0.250000"),
            execute=execute,
        )
    second = await cache.run(
        capability_id="cap",
        provider_id="prov",
        capability_params={"text": "retry"},
        estimated_cost_usd=Decimal("0.250000"),
        execute=execute,
    )

    assert second.value == "ok"
    assert second.hit is False
    assert calls == 2
    assert cache.entry_count == 1


def test_expensive_entries_get_longer_budget_ttl() -> None:
    clock = _ManualClock()
    cache = BudgetAwareSemanticCache[str](
        policy=_policy(
            default_ttl=dt.timedelta(seconds=10),
            high_value_estimate_usd=Decimal("1.000000"),
            high_value_ttl_multiplier=4,
        ),
        now=clock,
    )

    cheap_ttl = cache.policy.ttl_for(Decimal("0.999999"))
    expensive_ttl = cache.policy.ttl_for(Decimal("1.000000"))

    assert cheap_ttl == dt.timedelta(seconds=10)
    assert expensive_ttl == dt.timedelta(seconds=40)


def test_empty_custom_signature_is_rejected() -> None:
    hasher = _ConstantHasher("")

    with pytest.raises(ValueError, match="semantic signature must be non-empty"):
        build_semantic_cache_key(
            capability_id="cap",
            provider_id="prov",
            capability_params={"text": "prompt"},
            hasher=hasher,
        )


def test_canonical_hasher_signature_is_stable_for_dict_order() -> None:
    hasher = CanonicalSemanticHasher()

    assert hasher.signature(
        capability_id="cap",
        provider_id="prov",
        capability_params={"a": 1, "b": {"c": 2, "d": 3}},
    ) == hasher.signature(
        capability_id="cap",
        provider_id="prov",
        capability_params={"b": {"d": 3, "c": 2}, "a": 1},
    )
