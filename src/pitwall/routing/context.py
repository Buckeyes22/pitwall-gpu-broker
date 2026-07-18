"""Replay substrate for deterministic routing decisions."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, cast

from pitwall.core.models import Capability, Provider
from pitwall.runpod_client.availability import (
    AvailabilityCache,
    AvailabilityKey,
    get_global_availability_cache,
)

type ProviderSnapshot = Mapping[str, Any]
type ProviderInput = Provider | Mapping[str, Any]
type AvailabilityEntryInput = tuple[str, str, str, int, bool]


@dataclass(frozen=True, slots=True)
class AvailabilitySnapshotEntry:
    """One frozen RunPod availability value."""

    key: AvailabilityKey
    available: bool


@dataclass(frozen=True, slots=True)
class AvailabilitySnapshot:
    """Immutable availability snapshot used by Stage 4 capacity checks."""

    entries: tuple[AvailabilitySnapshotEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))

    @classmethod
    def empty(cls) -> AvailabilitySnapshot:
        return cls()

    @classmethod
    def from_entries(cls, entries: Iterable[AvailabilityEntryInput]) -> AvailabilitySnapshot:
        return cls(
            tuple(
                AvailabilitySnapshotEntry(
                    key=AvailabilityKey(
                        datacenter=datacenter,
                        gpu_name=gpu_name,
                        cloud_type=cloud_type.upper(),
                        gpu_count=gpu_count,
                    ),
                    available=available,
                )
                for datacenter, gpu_name, cloud_type, gpu_count, available in entries
            )
        )

    @classmethod
    def from_cache(cls, cache: AvailabilityCache) -> AvailabilitySnapshot:
        entries: list[AvailabilitySnapshotEntry] = []
        with cache._lock:
            for key, value in cache._cache.items():
                if cache._is_expired(value.checked_at):
                    continue
                entries.append(
                    AvailabilitySnapshotEntry(
                        key=key,
                        available=value.available,
                    )
                )
        return cls(tuple(entries))

    def is_available(
        self,
        datacenter: str,
        gpu_name: str,
        cloud_type: str,
        gpu_count: int,
    ) -> bool | None:
        key = AvailabilityKey(
            datacenter=datacenter,
            gpu_name=gpu_name,
            cloud_type=cloud_type.upper(),
            gpu_count=gpu_count,
        )
        for entry in reversed(self.entries):
            if entry.key == key:
                return entry.available
        return None


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """Frozen world snapshot for deterministic planner replay."""

    now: dt.datetime
    availability_snapshot: AvailabilitySnapshot = field(default_factory=AvailabilitySnapshot.empty)
    providers: tuple[ProviderSnapshot, ...] = field(default_factory=tuple)
    capability: Capability | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "now", _normalize_utc(self.now, field_name="now"))
        object.__setattr__(self, "providers", tuple(self.providers))
        if self.capability is not None:
            object.__setattr__(self, "capability", self.capability.model_copy(deep=True))

    @property
    def capacity_snapshot(self) -> AvailabilitySnapshot:
        """Alias for the availability data consumed by Stage 4 capacity checks."""

        return self.availability_snapshot

    @classmethod
    def live(
        cls,
        *,
        now: dt.datetime | None = None,
        availability_cache: AvailabilityCache | None = None,
        providers: Iterable[ProviderInput] = (),
        capability: Capability | None = None,
    ) -> PlanningContext:
        observed_at = dt.datetime.now(dt.UTC) if now is None else now
        cache = (
            availability_cache
            if availability_cache is not None
            else get_global_availability_cache()
        )
        return cls(
            now=observed_at,
            availability_snapshot=AvailabilitySnapshot.from_cache(cache),
            providers=freeze_provider_snapshot(providers),
            capability=capability,
        )

    @classmethod
    def replay(
        cls,
        *,
        now: dt.datetime,
        availability_snapshot: AvailabilitySnapshot | None = None,
        availability_entries: Iterable[AvailabilityEntryInput] = (),
        providers: Iterable[ProviderInput] = (),
        capability: Capability | None = None,
    ) -> PlanningContext:
        entries = tuple(availability_entries)
        if availability_snapshot is not None and entries:
            raise ValueError(
                "availability_snapshot and availability_entries are mutually exclusive"
            )
        snapshot = (
            availability_snapshot
            if availability_snapshot is not None
            else AvailabilitySnapshot.from_entries(entries)
        )
        return cls(
            now=now,
            availability_snapshot=snapshot,
            providers=freeze_provider_snapshot(providers),
            capability=capability,
        )


def freeze_provider_snapshot(providers: Iterable[ProviderInput]) -> tuple[ProviderSnapshot, ...]:
    """Return an immutable provider tuple detached from later source mutations."""

    return tuple(_freeze_provider(provider) for provider in providers)


def _freeze_provider(provider: ProviderInput) -> ProviderSnapshot:
    if isinstance(provider, Provider):
        return _freeze_mapping(provider.model_dump(mode="python"))
    return _freeze_mapping(provider)


def _freeze_mapping(mapping: Mapping[str, Any]) -> ProviderSnapshot:
    frozen = {key: _freeze_value(value) for key, value in mapping.items()}
    return cast(ProviderSnapshot, MappingProxyType(frozen))


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(cast(Mapping[str, Any], value))
    if isinstance(value, Enum):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _normalize_utc(value: dt.datetime, *, field_name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(dt.UTC)


__all__ = [
    "AvailabilitySnapshot",
    "AvailabilitySnapshotEntry",
    "PlanningContext",
    "ProviderInput",
    "ProviderSnapshot",
    "freeze_provider_snapshot",
]
