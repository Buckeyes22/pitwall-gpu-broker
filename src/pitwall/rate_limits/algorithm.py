"""Token-bucket admission logic for RunPod endpoint operations."""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pitwall.api.exceptions import RateLimited
from pitwall.core.models import RateBucket
from pitwall.rate_limits.retry_after import (
    DEFAULT_MAX_RETRY_AFTER_DELAY_S,
    parse_retry_after,
)

REFILL_WINDOW_S = 10.0
LOCAL_WAIT_LIMIT_S = 30.0
CAPACITY_REFRESH_INTERVAL_S = 30.0
CAPACITY_REBUILD_WINDOW_S = 60.0

SleepFunc = Callable[[float], Awaitable[object]]
MonotonicClock = Callable[[], float]
WallClock = Callable[[], dt.datetime]


class RateBucketStoreProtocol(Protocol):
    async def load_or_create(
        self,
        endpoint_id: str,
        operation: str,
        capacity: int,
    ) -> RateBucket: ...

    async def update_capacity(
        self,
        endpoint_id: str,
        operation: str,
        new_capacity: int,
    ) -> RateBucket: ...

    async def record_429(
        self,
        endpoint_id: str,
        operation: str,
        new_capacity: int | None = None,
    ) -> RateBucket: ...

    async def atomic_refill_consume(
        self,
        endpoint_id: str,
        operation: str,
        tokens_to_consume: float,
    ) -> tuple[RateBucket, bool]: ...


@dataclass(frozen=True)
class RateLimitConfig:
    """Capacity and wait settings for one endpoint operation."""

    base_limit: int
    per_worker_limit: int
    refill_window_s: float = REFILL_WINDOW_S
    max_local_wait_s: float = LOCAL_WAIT_LIMIT_S
    capacity_refresh_interval_s: float = CAPACITY_REFRESH_INTERVAL_S
    capacity_rebuild_window_s: float = CAPACITY_REBUILD_WINDOW_S

    def __post_init__(self) -> None:
        if self.base_limit <= 0:
            raise ValueError("base_limit must be > 0")
        if self.per_worker_limit < 0:
            raise ValueError("per_worker_limit must be >= 0")
        if self.refill_window_s <= 0:
            raise ValueError("refill_window_s must be > 0")
        if self.max_local_wait_s < 0:
            raise ValueError("max_local_wait_s must be >= 0")
        if self.capacity_refresh_interval_s <= 0:
            raise ValueError("capacity_refresh_interval_s must be > 0")
        if self.capacity_rebuild_window_s <= 0:
            raise ValueError("capacity_rebuild_window_s must be > 0")


def dynamic_capacity(
    *,
    base_limit: int,
    worker_count: int,
    per_worker_limit: int,
) -> int:
    """Return RunPod's effective per-operation capacity.

    RunPod's documented shape is ``max(base_limit, worker_count *
    per_worker_limit)``. A zero-worker health payload still keeps the base
    burst capacity so consumers do not divide by zero during cold starts.
    """

    if base_limit <= 0:
        raise ValueError("base_limit must be > 0")
    if worker_count < 0:
        raise ValueError("worker_count must be >= 0")
    if per_worker_limit < 0:
        raise ValueError("per_worker_limit must be >= 0")
    return max(base_limit, worker_count * per_worker_limit)


def halved_capacity(capacity: int) -> int:
    """Return the conservative integer capacity after a downstream 429."""

    if capacity <= 0:
        raise ValueError("capacity must be > 0")
    return max(1, capacity // 2)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _normalize_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def capacity_after_429(
    *,
    target_capacity: int,
    recent_429_at: dt.datetime | None,
    now: dt.datetime,
    rebuild_window_s: float = CAPACITY_REBUILD_WINDOW_S,
) -> int:
    """Return capacity after a 429, rebuilding linearly to target capacity."""

    if target_capacity <= 0:
        raise ValueError("target_capacity must be > 0")
    if rebuild_window_s <= 0:
        raise ValueError("rebuild_window_s must be > 0")
    if recent_429_at is None:
        return target_capacity

    elapsed_s = (_normalize_utc(now) - _normalize_utc(recent_429_at)).total_seconds()
    if elapsed_s >= rebuild_window_s:
        return target_capacity

    reduced_capacity = halved_capacity(target_capacity)
    if elapsed_s <= 0:
        return reduced_capacity

    recovered = reduced_capacity + (target_capacity - reduced_capacity) * (
        elapsed_s / rebuild_window_s
    )
    return min(target_capacity, max(reduced_capacity, math.floor(recovered)))


def refill_tokens(
    *,
    tokens: float,
    capacity: int,
    elapsed_s: float,
    refill_window_s: float = REFILL_WINDOW_S,
) -> float:
    """Refill token count using a full-bucket refill window."""

    if capacity <= 0:
        raise ValueError("capacity must be > 0")
    if tokens < 0:
        raise ValueError("tokens must be >= 0")
    if elapsed_s < 0:
        raise ValueError("elapsed_s must be >= 0")
    if refill_window_s <= 0:
        raise ValueError("refill_window_s must be > 0")

    refill_rate = capacity / refill_window_s
    return min(float(capacity), tokens + elapsed_s * refill_rate)


def seconds_until_available(
    *,
    tokens: float,
    capacity: int,
    tokens_needed: float = 1.0,
    refill_window_s: float = REFILL_WINDOW_S,
) -> float:
    """Return seconds until ``tokens_needed`` can be consumed."""

    if capacity <= 0:
        raise ValueError("capacity must be > 0")
    if tokens < 0:
        raise ValueError("tokens must be >= 0")
    if tokens_needed <= 0:
        raise ValueError("tokens_needed must be > 0")
    if refill_window_s <= 0:
        raise ValueError("refill_window_s must be > 0")

    if tokens >= tokens_needed:
        return 0.0
    if tokens_needed > capacity:
        return math.inf

    refill_rate = capacity / refill_window_s
    return (tokens_needed - tokens) / refill_rate


@dataclass
class TokenBucket:
    """In-memory token bucket useful for deterministic algorithm tests."""

    capacity: int
    tokens: float | None = None
    last_refilled_at_s: float | None = None
    refill_window_s: float = REFILL_WINDOW_S

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be > 0")
        if self.refill_window_s <= 0:
            raise ValueError("refill_window_s must be > 0")
        if self.tokens is None:
            self.tokens = float(self.capacity)
        if self.tokens < 0:
            raise ValueError("tokens must be >= 0")
        self.tokens = min(float(self.capacity), self.tokens)
        if self.last_refilled_at_s is None:
            self.last_refilled_at_s = time.monotonic()

    def refill(self, *, now_s: float | None = None) -> float:
        """Refill in-place and return the current token count."""

        current_s = time.monotonic() if now_s is None else now_s
        elapsed_s = max(0.0, current_s - self._last_refilled_at_s())
        self.tokens = refill_tokens(
            tokens=self._tokens(),
            capacity=self.capacity,
            elapsed_s=elapsed_s,
            refill_window_s=self.refill_window_s,
        )
        self.last_refilled_at_s = current_s
        return self.tokens

    def resize(self, capacity: int, *, now_s: float | None = None) -> None:
        """Apply a dynamic capacity change after refilling to ``now_s``."""

        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self.refill(now_s=now_s)
        self.capacity = capacity
        self.tokens = min(float(capacity), self._tokens())

    def try_consume(
        self,
        tokens: float = 1.0,
        *,
        now_s: float | None = None,
    ) -> bool:
        """Consume tokens if available after refilling to ``now_s``."""

        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        self.refill(now_s=now_s)
        if self._tokens() < tokens:
            return False
        self.tokens = self._tokens() - tokens
        return True

    def retry_after_s(self, tokens: float = 1.0) -> float:
        """Return seconds until ``tokens`` can be consumed."""

        return seconds_until_available(
            tokens=self._tokens(),
            capacity=self.capacity,
            tokens_needed=tokens,
            refill_window_s=self.refill_window_s,
        )

    def _tokens(self) -> float:
        if self.tokens is None:
            raise RuntimeError("TokenBucket was not initialized")
        return self.tokens

    def _last_refilled_at_s(self) -> float:
        if self.last_refilled_at_s is None:
            raise RuntimeError("TokenBucket was not initialized")
        return self.last_refilled_at_s


class TokenBucketRateLimiter:
    """Store-backed token-bucket admission with bounded local waiting."""

    def __init__(
        self,
        store: RateBucketStoreProtocol,
        config: RateLimitConfig,
        *,
        sleep: SleepFunc = asyncio.sleep,
        monotonic: MonotonicClock = time.monotonic,
        wall_clock: WallClock = _utc_now,
    ) -> None:
        self._store = store
        self._config = config
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_clock = wall_clock

    async def acquire(
        self,
        endpoint_id: str,
        operation: str,
        *,
        worker_count: int,
        tokens: float = 1.0,
    ) -> RateBucket:
        """Consume tokens or raise ``RateLimited`` after the local wait budget."""

        if tokens <= 0:
            raise ValueError("tokens must be > 0")

        capacity = self.capacity_for_workers(worker_count)
        await self._ensure_bucket_capacity(endpoint_id, operation, capacity)

        deadline = self._monotonic() + self._config.max_local_wait_s

        while True:
            bucket, allowed = await self._store.atomic_refill_consume(
                endpoint_id,
                operation,
                tokens,
            )
            if allowed:
                return bucket

            retry_after_s = seconds_until_available(
                tokens=bucket.tokens,
                capacity=bucket.capacity,
                tokens_needed=tokens,
                refill_window_s=self._config.refill_window_s,
            )
            remaining_wait_s = deadline - self._monotonic()
            if not math.isfinite(retry_after_s) or retry_after_s > remaining_wait_s:
                raise RateLimited(retry_after_s=retry_after_s)

            await self._sleep(max(0.0, retry_after_s))

    def capacity_for_workers(self, worker_count: int) -> int:
        return dynamic_capacity(
            base_limit=self._config.base_limit,
            worker_count=worker_count,
            per_worker_limit=self._config.per_worker_limit,
        )

    async def record_429(
        self,
        endpoint_id: str,
        operation: str,
        *,
        worker_count: int,
    ) -> RateBucket:
        """Record a downstream 429 and drop capacity to half the current target."""

        target_capacity = self.capacity_for_workers(worker_count)
        await self._ensure_bucket_capacity(endpoint_id, operation, target_capacity)
        return await self._store.record_429(
            endpoint_id,
            operation,
            halved_capacity(target_capacity),
        )

    async def _ensure_bucket_capacity(
        self,
        endpoint_id: str,
        operation: str,
        capacity: int,
    ) -> None:
        bucket = await self._store.load_or_create(endpoint_id, operation, capacity)
        effective_capacity = capacity_after_429(
            target_capacity=capacity,
            recent_429_at=bucket.recent_429_at,
            now=self._wall_clock(),
            rebuild_window_s=self._config.capacity_rebuild_window_s,
        )
        if bucket.capacity != effective_capacity:
            await self._store.update_capacity(endpoint_id, operation, effective_capacity)


RateLimiter = TokenBucketRateLimiter
RateLimitExceeded = RateLimited
effective_capacity = dynamic_capacity


__all__ = [
    "CAPACITY_REBUILD_WINDOW_S",
    "CAPACITY_REFRESH_INTERVAL_S",
    "DEFAULT_MAX_RETRY_AFTER_DELAY_S",
    "LOCAL_WAIT_LIMIT_S",
    "REFILL_WINDOW_S",
    "RateBucketStoreProtocol",
    "RateLimitConfig",
    "RateLimitExceeded",
    "RateLimited",
    "RateLimiter",
    "SleepFunc",
    "TokenBucket",
    "TokenBucketRateLimiter",
    "WallClock",
    "capacity_after_429",
    "dynamic_capacity",
    "effective_capacity",
    "refill_tokens",
    "halved_capacity",
    "parse_retry_after",
    "seconds_until_available",
]
