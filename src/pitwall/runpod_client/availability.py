"""5-minute TTL availability cache keyed by datacenter, GPU name, cloud type, and GPU count.

For pod leases only, RunPod GPU availability is volatile.  Pitwall pre-checks
against this cached availability map (refreshed every 5 min) and only attempts
pod creation on providers where the requested GPU class is currently available.

Cache key: (datacenter, gpu_name, cloud_type, gpu_count)
Cache value: {"available": bool, "checked_at": float}
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import ClassVar

DEFAULT_TTL_S = 300.0


@dataclass(frozen=True)
class AvailabilityKey:
    """Composite cache key for GPU availability."""

    datacenter: str
    gpu_name: str
    cloud_type: str
    gpu_count: int

    def __post_init__(self) -> None:
        if not self.datacenter:
            raise ValueError("datacenter must be non-empty")
        if not self.gpu_name:
            raise ValueError("gpu_name must be non-empty")
        if not self.cloud_type:
            raise ValueError("cloud_type must be non-empty")
        if self.gpu_count < 1:
            raise ValueError("gpu_count must be >= 1")


@dataclass
class AvailabilityValue:
    """Cached availability result with TTL tracking."""

    available: bool
    checked_at: float


@dataclass
class AvailabilityCache:
    """Thread-safe 5-minute TTL cache for GPU availability.

    Stores whether a given GPU configuration (datacenter + GPU type + cloud type +
    GPU count) is currently available according to RunPod's capacity API.
    """

    DEFAULT_TTL_S: ClassVar[float] = DEFAULT_TTL_S

    _cache: dict[AvailabilityKey, AvailabilityValue] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _ttl_s: float = DEFAULT_TTL_S

    def is_available(
        self,
        datacenter: str,
        gpu_name: str,
        cloud_type: str,
        gpu_count: int,
    ) -> bool | None:
        """Return cached availability, or None if missing or expired.

        Args:
            datacenter: RunPod datacenter/region id (e.g. "US-KS-2")
            gpu_name: Canonical GPU name (e.g. "NVIDIA H100 80GB HBM3")
            cloud_type: "SECURE", "COMMUNITY", or "ALL"
            gpu_count: Number of GPUs requested

        Returns:
            True if available, False if unavailable, None if not cached or expired.
        """
        key = AvailabilityKey(
            datacenter=datacenter,
            gpu_name=gpu_name,
            cloud_type=cloud_type.upper(),
            gpu_count=gpu_count,
        )
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if self._is_expired(entry.checked_at):
                del self._cache[key]
                return None
            return entry.available

    def set_available(
        self,
        datacenter: str,
        gpu_name: str,
        cloud_type: str,
        gpu_count: int,
        available: bool,
    ) -> None:
        """Update the cached availability for a GPU configuration.

        Args:
            datacenter: RunPod datacenter/region id
            gpu_name: Canonical GPU name
            cloud_type: "SECURE", "COMMUNITY", or "ALL"
            gpu_count: Number of GPUs
            available: Whether the GPU is available in this configuration
        """
        key = AvailabilityKey(
            datacenter=datacenter,
            gpu_name=gpu_name,
            cloud_type=cloud_type.upper(),
            gpu_count=gpu_count,
        )
        with self._lock:
            self._cache[key] = AvailabilityValue(
                available=available,
                checked_at=time.monotonic(),
            )

    def bulk_set_available(
        self,
        entries: list[tuple[str, str, str, int, bool]],
    ) -> None:
        """Update cached availability for multiple GPU configurations.

        Args:
            entries: List of (datacenter, gpu_name, cloud_type, gpu_count, available)
        """
        with self._lock:
            now = time.monotonic()
            for datacenter, gpu_name, cloud_type, gpu_count, available in entries:
                key = AvailabilityKey(
                    datacenter=datacenter,
                    gpu_name=gpu_name,
                    cloud_type=cloud_type.upper(),
                    gpu_count=gpu_count,
                )
                self._cache[key] = AvailabilityValue(
                    available=available,
                    checked_at=now,
                )

    def invalidate(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()

    def invalidate_key(
        self,
        datacenter: str,
        gpu_name: str,
        cloud_type: str,
        gpu_count: int,
    ) -> bool:
        """Remove a specific key from the cache.

        Returns:
            True if the key was present, False otherwise.
        """
        key = AvailabilityKey(
            datacenter=datacenter,
            gpu_name=gpu_name,
            cloud_type=cloud_type.upper(),
            gpu_count=gpu_count,
        )
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def cache_size(self) -> int:
        """Return the number of cached entries."""
        with self._lock:
            return len(self._cache)

    def expired_count(self) -> int:
        """Return the number of expired entries (does not remove them)."""
        with self._lock:
            expired = 0
            for entry in self._cache.values():
                if self._is_expired(entry.checked_at):
                    expired += 1
            return expired

    def sweep_expired(self) -> int:
        """Remove all expired entries.

        Returns:
            The number of entries removed.
        """
        with self._lock:
            expired_keys = [
                key for key, entry in self._cache.items() if self._is_expired(entry.checked_at)
            ]
            for key in expired_keys:
                del self._cache[key]
            return len(expired_keys)

    def _is_expired(self, checked_at: float) -> bool:
        """Check if a cache entry has exceeded its TTL."""
        return (time.monotonic() - checked_at) > self._ttl_s

    def __len__(self) -> int:
        return self.cache_size()


_global_cache: AvailabilityCache | None = None
_global_lock = threading.Lock()


def get_global_availability_cache() -> AvailabilityCache:
    """Return the process-global availability cache instance."""
    global _global_cache
    with _global_lock:
        if _global_cache is None:
            _global_cache = AvailabilityCache()
        return _global_cache


def reset_global_availability_cache() -> None:
    """Reset the global cache (primarily for testing)."""
    global _global_cache
    with _global_lock:
        if _global_cache is not None:
            _global_cache.invalidate()
        _global_cache = None


__all__ = [
    "AvailabilityCache",
    "AvailabilityKey",
    "AvailabilityValue",
    "DEFAULT_TTL_S",
    "get_global_availability_cache",
    "reset_global_availability_cache",
]
