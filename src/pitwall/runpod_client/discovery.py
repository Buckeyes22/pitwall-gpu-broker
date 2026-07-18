"""GPU-type + datacenter discovery service for the RunPod broker catalog.

Normalizes live GraphQL market reads into an immutable snapshot that feeds
routing (Stage-4 capacity checks) and external catalog APIs.  The snapshot is
deterministic and replay-friendly: callers may freeze it and pass it into
:class:`pitwall.routing.context.PlanningContext.replay` for counterfactual
planning.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from pitwall.routing.context import AvailabilityEntryInput, AvailabilitySnapshot
from pitwall.runpod_client.graphql import (
    RunpodDatacenter,
    RunpodGpuType,
    RunpodGraphQLClient,
)

DEFAULT_DISCOVERY_TTL_S = 60.0


@dataclass(frozen=True, slots=True)
class GpuCatalogEntry:
    """Normalized GPU type with live prices and datacenter presence."""

    gpu_type_id: str
    display_name: str | None = None
    manufacturer: str | None = None
    memory_in_gb: int | None = None
    cuda_cores: int | None = None
    secure_cloud: bool = False
    community_cloud: bool = False
    secure_price: Decimal | None = None
    community_price: Decimal | None = None
    secure_spot_price: Decimal | None = None
    community_spot_price: Decimal | None = None
    lowest_bid_price: Decimal | None = None
    uninterruptable_price: Decimal | None = None
    datacenter_ids: tuple[str, ...] = ()
    available_gpu_counts: tuple[int, ...] = ()
    stock_status: str | None = None
    max_gpu_count: int | None = None


@dataclass(frozen=True, slots=True)
class DatacenterCatalogEntry:
    """Normalized datacenter with the GPU lanes currently available there."""

    datacenter_id: str
    name: str | None = None
    location: str | None = None
    global_network: bool = False
    storage_support: bool = False
    listed: bool = False
    compliance: tuple[str, ...] = ()
    gpu_types: tuple[str, ...] = ()
    gpu_availability: Mapping[str, bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GpuDiscoverySnapshot:
    """Immutable catalog snapshot produced by :class:`GpuDiscoveryService`."""

    fetched_at: dt.datetime
    gpus: tuple[GpuCatalogEntry, ...] = ()
    datacenters: tuple[DatacenterCatalogEntry, ...] = ()

    def gpu_by_id(self, gpu_type_id: str) -> GpuCatalogEntry | None:
        for gpu in self.gpus:
            if gpu.gpu_type_id == gpu_type_id:
                return gpu
        return None

    def datacenter_by_id(self, datacenter_id: str) -> DatacenterCatalogEntry | None:
        for dc in self.datacenters:
            if dc.datacenter_id == datacenter_id:
                return dc
        return None

    def to_availability_entries(
        self,
        *,
        gpu_count: int = 1,
        cloud_type: str | None = None,
    ) -> list[AvailabilityEntryInput]:
        """Flatten the snapshot into tuples compatible with
        :class:`pitwall.routing.context.PlanningContext.replay`.
        """
        entries: list[AvailabilityEntryInput] = []
        for gpu in self.gpus:
            clouds = _cloud_types_for_gpu(gpu, cloud_type)
            for dc_id in gpu.datacenter_ids:
                dc = self.datacenter_by_id(dc_id)
                if dc is None:
                    continue
                available = dc.gpu_availability.get(gpu.gpu_type_id, False)
                counts = gpu.available_gpu_counts or (gpu_count,)
                for count in counts:
                    for cld in clouds:
                        entries.append((dc_id, gpu.gpu_type_id, cld, count, available))
        return entries

    def to_availability_snapshot(
        self,
        *,
        gpu_count: int = 1,
        cloud_type: str | None = None,
    ) -> AvailabilitySnapshot:
        """Return an :class:`AvailabilitySnapshot` for Stage-4 capacity checks."""
        return AvailabilitySnapshot.from_entries(
            self.to_availability_entries(gpu_count=gpu_count, cloud_type=cloud_type)
        )


class GpuDiscoveryService:
    """Async discovery service with TTL caching.

    Refresh is serialized behind an ``asyncio.Lock`` so concurrent callers
    share one GraphQL round-trip.
    """

    def __init__(
        self,
        graphql_client: RunpodGraphQLClient,
        *,
        ttl_s: float = DEFAULT_DISCOVERY_TTL_S,
    ) -> None:
        if ttl_s < 0:
            raise ValueError("ttl_s must be non-negative")
        self._client = graphql_client
        self._ttl_s = ttl_s
        self._snapshot: GpuDiscoverySnapshot | None = None
        self._last_fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self) -> GpuDiscoverySnapshot:
        """Fetch fresh GPU types and datacenters from RunPod GraphQL."""
        gpu_types = await self._client.gpu_types()
        datacenters = await self._client.datacenters()
        snapshot = _build_snapshot(gpu_types, datacenters)
        self._snapshot = snapshot
        self._last_fetched_at = asyncio.get_running_loop().time()
        return snapshot

    async def get_snapshot(self) -> GpuDiscoverySnapshot:
        """Return cached snapshot if still fresh, otherwise refresh."""
        if self._snapshot is not None and not self._is_stale():
            return self._snapshot
        async with self._lock:
            # Double-check after acquiring the lock.
            if self._snapshot is not None and not self._is_stale():
                return self._snapshot
            return await self.refresh()

    def get_gpu(self, gpu_type_id: str) -> GpuCatalogEntry | None:
        """Lookup a GPU entry from the current cached snapshot."""
        if self._snapshot is None:
            return None
        return self._snapshot.gpu_by_id(gpu_type_id)

    def get_datacenter(self, datacenter_id: str) -> DatacenterCatalogEntry | None:
        """Lookup a datacenter entry from the current cached snapshot."""
        if self._snapshot is None:
            return None
        return self._snapshot.datacenter_by_id(datacenter_id)

    def invalidate(self) -> None:
        """Force the next :meth:`get_snapshot` call to refresh."""
        self._last_fetched_at = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    def _is_stale(self) -> bool:
        if self._ttl_s == 0:
            return True
        elapsed = asyncio.get_running_loop().time() - self._last_fetched_at
        return elapsed > self._ttl_s


def _build_snapshot(
    gpu_types: Iterable[RunpodGpuType],
    datacenters: Iterable[RunpodDatacenter],
) -> GpuDiscoverySnapshot:
    now = dt.datetime.now(dt.UTC)
    gpu_entries = tuple(_normalize_gpu(gpu) for gpu in gpu_types)
    dc_entries = tuple(_normalize_datacenter(dc) for dc in datacenters)
    return GpuDiscoverySnapshot(
        fetched_at=now,
        gpus=gpu_entries,
        datacenters=dc_entries,
    )


def _normalize_gpu(gpu: RunpodGpuType) -> GpuCatalogEntry:
    dc_ids: list[str] = []
    counts: list[int] = []
    stock: str | None = None
    if gpu.lowest_price is not None:
        counts = list(gpu.lowest_price.available_gpu_counts or [])
        stock = gpu.lowest_price.stock_status
    for dc in gpu.node_group_datacenters:
        if dc.id:
            dc_ids.append(dc.id)
    return GpuCatalogEntry(
        gpu_type_id=gpu.id,
        display_name=gpu.display_name,
        manufacturer=gpu.manufacturer,
        memory_in_gb=gpu.memory_in_gb,
        cuda_cores=gpu.cuda_cores,
        secure_cloud=gpu.secure_cloud,
        community_cloud=gpu.community_cloud,
        secure_price=gpu.secure_price,
        community_price=gpu.community_price,
        secure_spot_price=gpu.secure_spot_price,
        community_spot_price=gpu.community_spot_price,
        lowest_bid_price=gpu.lowest_price.minimum_bid_price if gpu.lowest_price else None,
        uninterruptable_price=gpu.lowest_price.uninterruptable_price if gpu.lowest_price else None,
        datacenter_ids=tuple(sorted(set(dc_ids))),
        available_gpu_counts=tuple(sorted(set(counts))) if counts else (),
        stock_status=stock,
        max_gpu_count=gpu.max_gpu_count,
    )


def _normalize_datacenter(dc: RunpodDatacenter) -> DatacenterCatalogEntry:
    gpu_types: list[str] = []
    availability: dict[str, bool] = {}
    for avail in dc.gpu_availability:
        gti = avail.gpu_type_id or avail.id
        if gti:
            gpu_types.append(gti)
            availability[gti] = bool(avail.available)
    return DatacenterCatalogEntry(
        datacenter_id=dc.id,
        name=dc.name,
        location=dc.location,
        global_network=dc.global_network,
        storage_support=dc.storage_support,
        listed=dc.listed,
        compliance=tuple(dc.compliance),
        gpu_types=tuple(sorted(set(gpu_types))),
        gpu_availability=availability,
    )


def _cloud_types_for_gpu(gpu: GpuCatalogEntry, override: str | None) -> tuple[str, ...]:
    if override is not None:
        return (override.upper(),)
    clouds: list[str] = []
    if gpu.secure_cloud:
        clouds.append("SECURE")
    if gpu.community_cloud:
        clouds.append("COMMUNITY")
    return tuple(clouds) if clouds else ("SECURE",)


__all__ = [
    "DEFAULT_DISCOVERY_TTL_S",
    "DatacenterCatalogEntry",
    "GpuCatalogEntry",
    "GpuDiscoveryService",
    "GpuDiscoverySnapshot",
]
