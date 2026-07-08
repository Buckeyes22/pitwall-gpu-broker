"""Hermetic tests for token-bucket rate-limit admission."""

from __future__ import annotations

import datetime as dt
import math
import os
import time
from dataclasses import dataclass, field

import pytest

from pitwall.api.exceptions import RateLimited
from pitwall.core.models import RateBucket
from pitwall.rate_limits import (
    RateLimitConfig,
    RateLimiter,
    TokenBucket,
    capacity_after_429,
    dynamic_capacity,
    halved_capacity,
    parse_retry_after,
    refill_tokens,
    seconds_until_available,
)
from pitwall.rate_limits.algorithm import _normalize_utc, _utc_now


def test_dynamic_capacity_uses_base_or_worker_scaled_limit() -> None:
    assert dynamic_capacity(base_limit=10, worker_count=0, per_worker_limit=4) == 10
    assert dynamic_capacity(base_limit=10, worker_count=2, per_worker_limit=4) == 10
    assert dynamic_capacity(base_limit=10, worker_count=3, per_worker_limit=4) == 12


def test_dynamic_capacity_rejects_invalid_inputs_with_field_messages() -> None:
    with pytest.raises(ValueError, match="^base_limit must be > 0$"):
        dynamic_capacity(base_limit=0, worker_count=1, per_worker_limit=1)
    with pytest.raises(ValueError, match="^worker_count must be >= 0$"):
        dynamic_capacity(base_limit=1, worker_count=-1, per_worker_limit=1)
    with pytest.raises(ValueError, match="^per_worker_limit must be >= 0$"):
        dynamic_capacity(base_limit=1, worker_count=1, per_worker_limit=-1)


def test_retry_after_parser_accepts_seconds_and_http_dates() -> None:
    now = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)

    assert parse_retry_after("3", now=now, max_delay_s=10) == 3.0
    assert parse_retry_after("Thu, 28 May 2026 12:00:04 GMT", now=now, max_delay_s=10) == 4.0
    assert parse_retry_after("120", now=now, max_delay_s=10) == 10.0
    assert parse_retry_after("not a retry header", now=now) is None


def test_capacity_after_429_halves_then_rebuilds_linearly() -> None:
    recent_429_at = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)

    assert halved_capacity(20) == 10
    assert (
        capacity_after_429(
            target_capacity=20,
            recent_429_at=recent_429_at,
            now=recent_429_at,
        )
        == 10
    )
    assert (
        capacity_after_429(
            target_capacity=20,
            recent_429_at=recent_429_at,
            now=recent_429_at + dt.timedelta(seconds=30),
        )
        == 15
    )
    assert (
        capacity_after_429(
            target_capacity=20,
            recent_429_at=recent_429_at,
            now=recent_429_at + dt.timedelta(seconds=60),
        )
        == 20
    )


def test_capacity_after_429_validates_boundaries_and_short_rebuild_window() -> None:
    now = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)
    with pytest.raises(ValueError, match="^target_capacity must be > 0$"):
        capacity_after_429(target_capacity=0, recent_429_at=None, now=now)
    with pytest.raises(ValueError, match="^rebuild_window_s must be > 0$"):
        capacity_after_429(
            target_capacity=10,
            recent_429_at=None,
            now=now,
            rebuild_window_s=0,
        )

    assert (
        capacity_after_429(
            target_capacity=20,
            recent_429_at=None,
            now=now,
            rebuild_window_s=1,
        )
        == 20
    )
    assert (
        capacity_after_429(
            target_capacity=20,
            recent_429_at=now,
            now=now + dt.timedelta(seconds=1),
            rebuild_window_s=2,
        )
        == 15
    )
    assert (
        capacity_after_429(
            target_capacity=20,
            recent_429_at=now,
            now=now + dt.timedelta(seconds=2),
            rebuild_window_s=2,
        )
        == 20
    )


def test_utc_helpers_return_aware_utc_datetimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _utc_now()
    assert now.tzinfo is dt.UTC

    naive = dt.datetime(2026, 5, 28, 12, 0, 0)
    assert _normalize_utc(naive) == dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)

    offset = dt.timezone(dt.timedelta(hours=-4))
    assert _normalize_utc(dt.datetime(2026, 5, 28, 8, 0, 0, tzinfo=offset)) == dt.datetime(
        2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC
    )

    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert _normalize_utc(dt.datetime(2026, 5, 28, 8, 0, 0, tzinfo=offset)).tzinfo is dt.UTC
    finally:
        if previous_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", previous_tz)
        time.tzset()


def test_refill_reaches_full_capacity_in_ten_seconds() -> None:
    assert refill_tokens(tokens=0, capacity=20, elapsed_s=5) == 10
    assert refill_tokens(tokens=0, capacity=20, elapsed_s=10) == 20
    assert refill_tokens(tokens=19, capacity=20, elapsed_s=10) == 20


def test_halved_capacity_handles_minimum_and_odd_integer_capacity() -> None:
    assert halved_capacity(1) == 1
    assert halved_capacity(3) == 1
    with pytest.raises(ValueError, match="^capacity must be > 0$"):
        halved_capacity(0)


def test_refill_tokens_validates_inputs_and_allows_one_second_window() -> None:
    with pytest.raises(ValueError, match="^capacity must be > 0$"):
        refill_tokens(tokens=0, capacity=0, elapsed_s=1)
    with pytest.raises(ValueError, match="^tokens must be >= 0$"):
        refill_tokens(tokens=-1, capacity=1, elapsed_s=1)
    with pytest.raises(ValueError, match="^elapsed_s must be >= 0$"):
        refill_tokens(tokens=0, capacity=1, elapsed_s=-1)
    with pytest.raises(ValueError, match="^refill_window_s must be > 0$"):
        refill_tokens(tokens=0, capacity=1, elapsed_s=1, refill_window_s=0)

    assert refill_tokens(tokens=0, capacity=4, elapsed_s=0.25, refill_window_s=1) == 1


def test_seconds_until_available_uses_ten_second_refill_window() -> None:
    assert seconds_until_available(tokens=0, capacity=20, tokens_needed=4) == 2
    assert seconds_until_available(tokens=3, capacity=20, tokens_needed=4) == 0.5
    assert seconds_until_available(tokens=4, capacity=20, tokens_needed=4) == 0


def test_seconds_until_available_validates_inputs_and_defaults_to_one_token() -> None:
    with pytest.raises(ValueError, match="^capacity must be > 0$"):
        seconds_until_available(tokens=0, capacity=0)
    with pytest.raises(ValueError, match="^tokens must be >= 0$"):
        seconds_until_available(tokens=-1, capacity=1)
    with pytest.raises(ValueError, match="^tokens_needed must be > 0$"):
        seconds_until_available(tokens=0, capacity=1, tokens_needed=0)
    with pytest.raises(ValueError, match="^refill_window_s must be > 0$"):
        seconds_until_available(tokens=0, capacity=1, refill_window_s=0)

    assert seconds_until_available(tokens=1, capacity=10) == 0
    assert seconds_until_available.__kwdefaults__ == {
        "tokens_needed": 1.0,
        "refill_window_s": 10.0,
    }
    assert (
        seconds_until_available(
            tokens=0,
            capacity=4,
            tokens_needed=1,
            refill_window_s=1,
        )
        == 0.25
    )
    assert seconds_until_available(tokens=11, capacity=10, tokens_needed=11) == 0


def test_in_memory_bucket_consumes_refills_and_resizes() -> None:
    bucket = TokenBucket(capacity=10, tokens=10, last_refilled_at_s=0)

    assert bucket.try_consume(10, now_s=0) is True
    assert bucket.try_consume(1, now_s=0) is False
    assert bucket.retry_after_s() == 1

    assert bucket.try_consume(1, now_s=1) is True
    assert bucket.tokens == 0

    bucket.resize(2, now_s=11)
    assert bucket.capacity == 2
    assert bucket.tokens == 2


@dataclass
class _FakeClock:
    now_s: float = 0.0
    sleep_delays: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now_s

    def wall_clock(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.now_s, dt.UTC)

    async def sleep(self, delay_s: float) -> None:
        self.sleep_delays.append(delay_s)
        self.now_s += delay_s


class _FakeBucketStore:
    def __init__(
        self,
        clock: _FakeClock,
        *,
        capacity: int = 10,
        tokens: float = 10.0,
        refill_window_s: float = 10.0,
    ) -> None:
        self.clock = clock
        self.refill_window_s = refill_window_s
        self.bucket = RateBucket(
            endpoint_id="endpoint-1",
            operation="runsync",
            capacity=capacity,
            tokens=tokens,
            last_refilled_at=dt.datetime.fromtimestamp(0, dt.UTC),
            recent_429_at=None,
        )
        self.capacity_updates: list[int] = []
        self.capacity_update_requests: list[tuple[str, str, int]] = []
        self.recorded_429_capacities: list[int] = []
        self.record_429_requests: list[tuple[str, str, int | None]] = []
        self.load_requests: list[tuple[str, str, int]] = []
        self.consume_requests: list[tuple[str, str, float]] = []
        self.consume_calls = 0

    async def load_or_create(
        self,
        endpoint_id: str,
        operation: str,
        capacity: int,
    ) -> RateBucket:
        self.load_requests.append((endpoint_id, operation, capacity))
        if self.bucket.endpoint_id != endpoint_id or self.bucket.operation != operation:
            self.bucket = RateBucket(
                endpoint_id=endpoint_id,
                operation=operation,
                capacity=capacity,
                tokens=float(capacity),
                last_refilled_at=dt.datetime.fromtimestamp(self.clock.now_s, dt.UTC),
                recent_429_at=None,
            )
        return self.bucket

    async def update_capacity(
        self,
        endpoint_id: str,
        operation: str,
        new_capacity: int,
    ) -> RateBucket:
        self.capacity_updates.append(new_capacity)
        self.capacity_update_requests.append((endpoint_id, operation, new_capacity))
        elapsed_s = self.clock.now_s - self.bucket.last_refilled_at.timestamp()
        refilled_tokens = refill_tokens(
            tokens=self.bucket.tokens,
            capacity=self.bucket.capacity,
            elapsed_s=max(0.0, elapsed_s),
            refill_window_s=self.refill_window_s,
        )
        self.bucket = self.bucket.model_copy(
            update={
                "capacity": new_capacity,
                "tokens": min(refilled_tokens, float(new_capacity)),
                "last_refilled_at": dt.datetime.fromtimestamp(
                    self.clock.now_s,
                    dt.UTC,
                ),
            }
        )
        return self.bucket

    async def record_429(
        self,
        endpoint_id: str,
        operation: str,
        new_capacity: int | None = None,
    ) -> RateBucket:
        self.record_429_requests.append((endpoint_id, operation, new_capacity))
        reduced_capacity = halved_capacity(self.bucket.capacity)
        if new_capacity is not None:
            reduced_capacity = min(reduced_capacity, new_capacity)
        self.recorded_429_capacities.append(reduced_capacity)

        elapsed_s = self.clock.now_s - self.bucket.last_refilled_at.timestamp()
        refilled_tokens = refill_tokens(
            tokens=self.bucket.tokens,
            capacity=self.bucket.capacity,
            elapsed_s=max(0.0, elapsed_s),
            refill_window_s=self.refill_window_s,
        )
        self.bucket = self.bucket.model_copy(
            update={
                "capacity": reduced_capacity,
                "tokens": min(refilled_tokens, float(reduced_capacity)),
                "last_refilled_at": dt.datetime.fromtimestamp(
                    self.clock.now_s,
                    dt.UTC,
                ),
                "recent_429_at": dt.datetime.fromtimestamp(
                    self.clock.now_s,
                    dt.UTC,
                ),
            }
        )
        return self.bucket

    async def atomic_refill_consume(
        self,
        endpoint_id: str,
        operation: str,
        tokens_to_consume: float,
    ) -> tuple[RateBucket, bool]:
        self.consume_calls += 1
        self.consume_requests.append((endpoint_id, operation, tokens_to_consume))
        elapsed_s = self.clock.now_s - self.bucket.last_refilled_at.timestamp()
        refilled_tokens = refill_tokens(
            tokens=self.bucket.tokens,
            capacity=self.bucket.capacity,
            elapsed_s=max(0.0, elapsed_s),
            refill_window_s=self.refill_window_s,
        )
        allowed = refilled_tokens >= tokens_to_consume
        final_tokens = refilled_tokens - tokens_to_consume if allowed else refilled_tokens
        self.bucket = self.bucket.model_copy(
            update={
                "tokens": final_tokens,
                "last_refilled_at": dt.datetime.fromtimestamp(
                    self.clock.now_s,
                    dt.UTC,
                ),
            }
        )
        return self.bucket, allowed


@pytest.mark.anyio
async def test_limiter_waits_locally_until_tokens_refill() -> None:
    clock = _FakeClock()
    store = _FakeBucketStore(clock, capacity=10, tokens=0)
    limiter = RateLimiter(
        store,
        RateLimitConfig(base_limit=10, per_worker_limit=1),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    bucket = await limiter.acquire(
        "endpoint-1",
        "runsync",
        worker_count=1,
    )

    assert store.consume_calls == 2
    assert clock.now_s == 1
    assert bucket.tokens == 0


@pytest.mark.anyio
async def test_limiter_passes_endpoint_operation_and_requested_tokens_to_store() -> None:
    clock = _FakeClock()
    store = _FakeBucketStore(clock, capacity=10, tokens=10)
    limiter = RateLimiter(
        store,
        RateLimitConfig(base_limit=10, per_worker_limit=1),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    bucket = await limiter.acquire(
        "endpoint-custom",
        "status",
        worker_count=1,
        tokens=2.5,
    )

    assert store.load_requests == [("endpoint-custom", "status", 10)]
    assert store.consume_requests == [("endpoint-custom", "status", 2.5)]
    assert bucket.tokens == 7.5
    assert RateLimiter.acquire.__kwdefaults__ == {"tokens": 1.0}


@pytest.mark.anyio
async def test_limiter_rejects_zero_token_acquire_with_message() -> None:
    clock = _FakeClock()
    store = _FakeBucketStore(clock)
    limiter = RateLimiter(
        store,
        RateLimitConfig(base_limit=10, per_worker_limit=1),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(ValueError, match="^tokens must be > 0$"):
        await limiter.acquire("endpoint-1", "runsync", worker_count=1, tokens=0)


@pytest.mark.anyio
async def test_limiter_allows_retry_exactly_at_wait_budget() -> None:
    clock = _FakeClock()
    store = _FakeBucketStore(clock, capacity=10, tokens=0)
    limiter = RateLimiter(
        store,
        RateLimitConfig(base_limit=10, per_worker_limit=1, max_local_wait_s=1),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    bucket = await limiter.acquire("endpoint-1", "runsync", worker_count=1)

    assert clock.sleep_delays == [1.0]
    assert bucket.tokens == 0


@pytest.mark.anyio
async def test_limiter_sleeps_zero_when_store_denies_available_tokens() -> None:
    class _DenyOnceWithAvailableTokensStore(_FakeBucketStore):
        async def atomic_refill_consume(
            self,
            endpoint_id: str,
            operation: str,
            tokens_to_consume: float,
        ) -> tuple[RateBucket, bool]:
            if self.consume_calls == 0:
                self.consume_calls += 1
                self.consume_requests.append((endpoint_id, operation, tokens_to_consume))
                return self.bucket, False
            return await super().atomic_refill_consume(
                endpoint_id,
                operation,
                tokens_to_consume,
            )

    clock = _FakeClock()
    store = _DenyOnceWithAvailableTokensStore(clock, capacity=10, tokens=1)
    limiter = RateLimiter(
        store,
        RateLimitConfig(base_limit=10, per_worker_limit=1),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    bucket = await limiter.acquire("endpoint-1", "runsync", worker_count=1)

    assert clock.sleep_delays == [0.0]
    assert bucket.tokens == 0.0


@pytest.mark.anyio
async def test_limiter_waits_for_requested_multi_token_shortfall_with_custom_window() -> None:
    clock = _FakeClock()
    store = _FakeBucketStore(clock, capacity=10, tokens=0, refill_window_s=5)
    limiter = RateLimiter(
        store,
        RateLimitConfig(
            base_limit=10,
            per_worker_limit=1,
            refill_window_s=5,
            max_local_wait_s=3,
        ),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    bucket = await limiter.acquire(
        "endpoint-1",
        "runsync",
        worker_count=1,
        tokens=4,
    )

    assert clock.sleep_delays == [2.0]
    assert bucket.tokens == 0.0


@pytest.mark.anyio
async def test_limiter_raises_when_repeated_denials_exhaust_wait_budget() -> None:
    class _AlwaysDenyStore(_FakeBucketStore):
        async def atomic_refill_consume(
            self,
            endpoint_id: str,
            operation: str,
            tokens_to_consume: float,
        ) -> tuple[RateBucket, bool]:
            self.consume_calls += 1
            self.consume_requests.append((endpoint_id, operation, tokens_to_consume))
            self.bucket = self.bucket.model_copy(
                update={
                    "tokens": 0.0,
                    "last_refilled_at": dt.datetime.fromtimestamp(
                        self.clock.now_s,
                        dt.UTC,
                    ),
                }
            )
            return self.bucket, False

    clock = _FakeClock()
    store = _AlwaysDenyStore(clock, capacity=1, tokens=0)
    sleep_delays: list[float] = []

    async def sleep_once(delay_s: float) -> None:
        sleep_delays.append(delay_s)
        if len(sleep_delays) > 1:
            raise AssertionError("limiter slept beyond the local wait budget")
        clock.now_s += delay_s

    limiter = RateLimiter(
        store,
        RateLimitConfig(base_limit=1, per_worker_limit=0, max_local_wait_s=15),
        sleep=sleep_once,
        monotonic=clock.monotonic,
    )

    with pytest.raises(RateLimited):
        await limiter.acquire("endpoint-1", "runsync", worker_count=0)

    assert sleep_delays == [10.0]


@pytest.mark.anyio
async def test_limiter_resizes_capacity_from_worker_count_before_consuming() -> None:
    clock = _FakeClock()
    store = _FakeBucketStore(clock, capacity=2, tokens=2)
    limiter = RateLimiter(
        store,
        RateLimitConfig(base_limit=2, per_worker_limit=3),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    bucket = await limiter.acquire(
        "endpoint-1",
        "runsync",
        worker_count=4,
    )

    assert store.capacity_updates == [12]
    assert bucket.capacity == 12
    assert bucket.tokens == 1


@pytest.mark.anyio
async def test_limiter_uses_configured_capacity_rebuild_window_and_update_keys() -> None:
    clock = _FakeClock(now_s=1)
    store = _FakeBucketStore(clock, capacity=10, tokens=10)
    store.bucket = store.bucket.model_copy(
        update={
            "recent_429_at": dt.datetime.fromtimestamp(0, dt.UTC),
            "last_refilled_at": dt.datetime.fromtimestamp(0, dt.UTC),
        }
    )
    limiter = RateLimiter(
        store,
        RateLimitConfig(
            base_limit=20,
            per_worker_limit=0,
            capacity_rebuild_window_s=2,
        ),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        wall_clock=clock.wall_clock,
    )

    bucket = await limiter.acquire("endpoint-1", "runsync", worker_count=0)

    assert store.capacity_update_requests == [("endpoint-1", "runsync", 15)]
    assert bucket.capacity == 15


@pytest.mark.anyio
async def test_limiter_records_429_then_rebuilds_capacity_over_sixty_seconds() -> None:
    clock = _FakeClock()
    store = _FakeBucketStore(clock, capacity=20, tokens=20)
    limiter = RateLimiter(
        store,
        RateLimitConfig(base_limit=20, per_worker_limit=0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        wall_clock=clock.wall_clock,
    )

    bucket = await limiter.record_429(
        "endpoint-1",
        "runsync",
        worker_count=0,
    )

    assert store.recorded_429_capacities == [10]
    assert store.record_429_requests == [("endpoint-1", "runsync", 10)]
    assert bucket.capacity == 10
    assert bucket.tokens == 10

    clock.now_s = 30
    bucket = await limiter.acquire(
        "endpoint-1",
        "runsync",
        worker_count=0,
    )

    assert store.capacity_updates == [15]
    assert bucket.capacity == 15
    assert bucket.tokens == 9

    clock.now_s = 60
    bucket = await limiter.acquire(
        "endpoint-1",
        "runsync",
        worker_count=0,
    )

    assert store.capacity_updates == [15, 20]
    assert bucket.capacity == 20
    assert bucket.tokens == 14


@pytest.mark.anyio
async def test_limiter_raises_503_shaped_rate_limited_after_wait_budget() -> None:
    clock = _FakeClock()
    store = _FakeBucketStore(clock, capacity=1, tokens=0)
    limiter = RateLimiter(
        store,
        RateLimitConfig(base_limit=1, per_worker_limit=0, max_local_wait_s=0.5),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(RateLimited) as exc_info:
        await limiter.acquire("endpoint-1", "runsync", worker_count=99)

    assert exc_info.value.status_code == 503
    assert exc_info.value.to_response_body() == {
        "error": "rate_limited",
        "retry_after_s": 10,
    }


def test_impossible_token_request_has_infinite_retry_after() -> None:
    assert math.isinf(seconds_until_available(tokens=0, capacity=1, tokens_needed=2))
