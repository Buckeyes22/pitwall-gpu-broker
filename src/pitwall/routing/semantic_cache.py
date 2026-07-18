"""Budget-aware semantic result cache for inference routing."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

from pitwall.routing.coalescing import AsyncRequestCoalescer

_WHITESPACE_RE = re.compile(r"\s+")
_KEY_VERSION = "v1"

type DecimalInput = Decimal | str | int | float


@runtime_checkable
class SemanticSignatureHasher(Protocol):
    """Produces a semantic signature for one resolved inference request."""

    def signature(
        self,
        *,
        capability_id: str,
        provider_id: str,
        capability_params: Mapping[str, Any],
    ) -> str: ...


class CanonicalSemanticHasher:
    """Canonical content hasher for deterministic prompt/result reuse.

    It collapses whitespace in strings, preserves sequence order, sorts mapping
    keys, and hashes the canonical JSON payload. Callers can replace this with
    an embedding-aware hasher while keeping the cache storage policy unchanged.
    """

    def signature(
        self,
        *,
        capability_id: str,
        provider_id: str,
        capability_params: Mapping[str, Any],
    ) -> str:
        normalized = _normalize_semantic_value(capability_params)
        return _sha256_json(normalized)


@dataclass(frozen=True, init=False)
class SemanticCachePolicy:
    """Budget policy for semantic cache admission and retention."""

    default_ttl: dt.timedelta
    max_entries: int
    min_cache_estimate_usd: Decimal
    high_value_estimate_usd: Decimal
    high_value_ttl_multiplier: int

    def __init__(
        self,
        *,
        default_ttl: dt.timedelta = dt.timedelta(minutes=30),
        max_entries: int = 1024,
        min_cache_estimate_usd: DecimalInput = Decimal("0.001000"),
        high_value_estimate_usd: DecimalInput = Decimal("0.100000"),
        high_value_ttl_multiplier: int = 4,
    ) -> None:
        if default_ttl <= dt.timedelta(0):
            raise ValueError("default_ttl must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if high_value_ttl_multiplier < 1:
            raise ValueError("high_value_ttl_multiplier must be at least 1")

        min_estimate = _non_negative_decimal(
            min_cache_estimate_usd,
            "min_cache_estimate_usd",
        )
        high_value_estimate = _non_negative_decimal(
            high_value_estimate_usd,
            "high_value_estimate_usd",
        )
        object.__setattr__(self, "default_ttl", default_ttl)
        object.__setattr__(self, "max_entries", max_entries)
        object.__setattr__(self, "min_cache_estimate_usd", min_estimate)
        object.__setattr__(self, "high_value_estimate_usd", high_value_estimate)
        object.__setattr__(self, "high_value_ttl_multiplier", high_value_ttl_multiplier)

    def should_cache(self, estimated_cost_usd: DecimalInput) -> bool:
        """Return True when the cost estimate is worth retaining."""

        estimate = _non_negative_decimal(estimated_cost_usd, "estimated_cost_usd")
        return estimate >= self.min_cache_estimate_usd

    def ttl_for(self, estimated_cost_usd: DecimalInput) -> dt.timedelta:
        """Return the entry TTL, extending retention for high-value work."""

        estimate = _non_negative_decimal(estimated_cost_usd, "estimated_cost_usd")
        if estimate >= self.high_value_estimate_usd:
            return self.default_ttl * self.high_value_ttl_multiplier
        return self.default_ttl


@dataclass(frozen=True)
class SemanticCacheRunResult[T]:
    """Result of a cache-wrapped execution."""

    value: T
    hit: bool
    key: str


@dataclass(frozen=True)
class _CacheLookup[T]:
    value: T


@dataclass
class _CacheEntry[T]:
    value: T
    estimated_cost_usd: Decimal
    expires_at: dt.datetime
    created_at: dt.datetime
    last_accessed_at: dt.datetime
    sequence: int


class BudgetAwareSemanticCache[T]:
    """Process-local semantic cache that keeps expensive results longest."""

    def __init__(
        self,
        *,
        policy: SemanticCachePolicy | None = None,
        hasher: SemanticSignatureHasher | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.policy = policy or SemanticCachePolicy()
        self.hasher = hasher or CanonicalSemanticHasher()
        self._now = now or _utc_now
        self._lock = asyncio.Lock()
        self._entries: dict[str, _CacheEntry[T]] = {}
        self._misses = AsyncRequestCoalescer[SemanticCacheRunResult[T]]()
        self._sequence = 0

    @property
    def entry_count(self) -> int:
        """Return the current number of retained entries."""

        return len(self._entries)

    async def run(
        self,
        *,
        capability_id: str,
        provider_id: str,
        capability_params: Mapping[str, Any],
        estimated_cost_usd: DecimalInput,
        execute: Callable[[], Awaitable[T]],
    ) -> SemanticCacheRunResult[T]:
        """Return a cached value or execute and optionally retain the result.

        The caller should pass an ``execute`` function that performs budget
        admission and provider I/O. A cache hit returns before ``execute`` is
        called, avoiding a second admission/spend path for the same semantic
        signature.
        """

        key = build_semantic_cache_key(
            capability_id=capability_id,
            provider_id=provider_id,
            capability_params=capability_params,
            hasher=self.hasher,
        )
        cached = await self._lookup(key)
        if cached is not None:
            return SemanticCacheRunResult(value=cached.value, hit=True, key=key)

        return await self._misses.run(
            key,
            lambda: self._execute_and_store(
                key=key,
                estimated_cost_usd=estimated_cost_usd,
                execute=execute,
            ),
        )

    async def _execute_and_store(
        self,
        *,
        key: str,
        estimated_cost_usd: DecimalInput,
        execute: Callable[[], Awaitable[T]],
    ) -> SemanticCacheRunResult[T]:
        cached = await self._lookup(key)
        if cached is not None:
            return SemanticCacheRunResult(value=cached.value, hit=True, key=key)

        value = await execute()
        await self.store(
            key=key,
            value=value,
            estimated_cost_usd=estimated_cost_usd,
        )
        return SemanticCacheRunResult(value=value, hit=False, key=key)

    async def store(
        self,
        *,
        key: str,
        value: T,
        estimated_cost_usd: DecimalInput,
    ) -> None:
        """Store a value if the budget policy admits it."""

        if key == "":
            raise ValueError("semantic cache key must be non-empty")
        estimate = _non_negative_decimal(estimated_cost_usd, "estimated_cost_usd")
        if not self.policy.should_cache(estimate):
            return

        now = _ensure_utc(self._now(), "now")
        expires_at = now + self.policy.ttl_for(estimate)
        async with self._lock:
            self._purge_expired_locked(now)
            self._sequence += 1
            self._entries[key] = _CacheEntry(
                value=value,
                estimated_cost_usd=estimate,
                expires_at=expires_at,
                created_at=now,
                last_accessed_at=now,
                sequence=self._sequence,
            )
            self._evict_to_size_locked()

    async def _lookup(self, key: str) -> _CacheLookup[T] | None:
        now = _ensure_utc(self._now(), "now")
        async with self._lock:
            self._purge_expired_locked(now)
            entry = self._entries.get(key)
            if entry is None:
                return None
            entry.last_accessed_at = now
            return _CacheLookup(value=entry.value)

    def _purge_expired_locked(self, now: dt.datetime) -> None:
        expired_keys = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired_keys:
            del self._entries[key]

    def _evict_to_size_locked(self) -> None:
        while len(self._entries) > self.policy.max_entries:
            victim_key = min(
                self._entries,
                key=lambda key: (
                    self._entries[key].estimated_cost_usd,
                    self._entries[key].last_accessed_at,
                    self._entries[key].created_at,
                    self._entries[key].sequence,
                    key,
                ),
            )
            del self._entries[victim_key]


def build_semantic_cache_key(
    *,
    capability_id: str,
    provider_id: str,
    capability_params: Mapping[str, Any],
    hasher: SemanticSignatureHasher | None = None,
) -> str:
    """Build a stable key that never embeds raw prompt content."""

    if capability_id.strip() == "":
        raise ValueError("capability_id must be non-empty")
    if provider_id.strip() == "":
        raise ValueError("provider_id must be non-empty")

    selected_hasher = hasher or CanonicalSemanticHasher()
    signature = selected_hasher.signature(
        capability_id=capability_id,
        provider_id=provider_id,
        capability_params=capability_params,
    )
    if signature.strip() == "":
        raise ValueError("semantic signature must be non-empty")

    namespace_digest = _sha256_json(
        {
            "capability_id": capability_id,
            "provider_id": provider_id,
        }
    )
    signature_digest = _sha256_text(signature)
    return f"semantic:{_KEY_VERSION}:{namespace_digest}:{signature_digest}"


def _normalize_semantic_value(value: Any) -> Any:
    if isinstance(value, str):
        return _WHITESPACE_RE.sub(" ", value.strip())
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_semantic_value(nested)
            for key, nested in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_semantic_value(item) for item in value]
    return value


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _ensure_utc(value: dt.datetime, name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(dt.UTC)


def _non_negative_decimal(raw_value: DecimalInput, name: str) -> Decimal:
    value = _decimal(raw_value, name)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _decimal(raw_value: DecimalInput, name: str) -> Decimal:
    if isinstance(raw_value, bool):
        raise ValueError(f"{name} must be a decimal value")
    try:
        value = Decimal(str(raw_value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal value") from exc
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


__all__ = [
    "BudgetAwareSemanticCache",
    "CanonicalSemanticHasher",
    "SemanticCachePolicy",
    "SemanticCacheRunResult",
    "SemanticSignatureHasher",
    "build_semantic_cache_key",
]
