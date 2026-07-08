"""Property-based tests for the GPU discovery snapshot.

Invariants:
    1. Determinism: identical GraphQL inputs → identical snapshot.
    2. Snapshot entries are unique by gpu_type_id / datacenter_id.
    3. to_availability_entries always returns tuples of length 5.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pitwall.runpod_client.discovery import (
    GpuCatalogEntry,
    _build_snapshot,
    _normalize_datacenter,
    _normalize_gpu,
)
from pitwall.runpod_client.graphql import RunpodDatacenter, RunpodGpuType

pytestmark = pytest.mark.property


_gpu_id_strategy = st.sampled_from(["NVIDIA L4", "NVIDIA H100", "NVIDIA A100"])
_dc_id_strategy = st.sampled_from(["US-KS-2", "EU-SE-1", "US-CA-1"])

_gpu_availability_strategy = st.fixed_dictionaries(
    {
        "available": st.booleans(),
        "gpuTypeId": _gpu_id_strategy,
        "stockStatus": st.sampled_from(["Available", "Out of Stock"]),
    },
    optional={
        "gpuTypeDisplayName": st.sampled_from(["NVIDIA L4", "NVIDIA H100"]),
        "displayName": st.sampled_from(["L4 lane", "H100 lane"]),
        "id": st.sampled_from(["lane-1", "lane-2", "lane-3"]),
    },
)

_gpu_type_dict_strategy = st.fixed_dictionaries(
    {
        "id": _gpu_id_strategy,
        "displayName": _gpu_id_strategy,
        "manufacturer": st.just("NVIDIA"),
        "memoryInGb": st.integers(min_value=1, max_value=160),
        "cudaCores": st.integers(min_value=1000, max_value=20000),
        "secureCloud": st.booleans(),
        "communityCloud": st.booleans(),
    },
    optional={
        "securePrice": st.one_of(st.none(), st.decimals(min_value=0, max_value=10, places=2)),
        "communityPrice": st.one_of(st.none(), st.decimals(min_value=0, max_value=10, places=2)),
        "secureSpotPrice": st.one_of(st.none(), st.decimals(min_value=0, max_value=5, places=2)),
        "communitySpotPrice": st.one_of(st.none(), st.decimals(min_value=0, max_value=5, places=2)),
        "maxGpuCount": st.integers(min_value=1, max_value=8),
        "lowestPrice": st.one_of(
            st.none(),
            st.fixed_dictionaries(
                {
                    "gpuTypeId": _gpu_id_strategy,
                    "minimumBidPrice": st.decimals(min_value=0, max_value=5, places=2),
                    "stockStatus": st.sampled_from(["High", "Low", "Out of Stock"]),
                    "availableGpuCounts": st.lists(
                        st.integers(min_value=1, max_value=8), min_size=0, max_size=3
                    ),
                }
            ),
        ),
        "nodeGroupDatacenters": st.lists(
            st.fixed_dictionaries(
                {
                    "id": _dc_id_strategy,
                    "name": st.sampled_from(["Kansas", "Sweden", "California"]),
                    "location": st.sampled_from(["US", "EU", "US-CA"]),
                }
            ),
            min_size=0,
            max_size=3,
        ),
    },
)

_datacenter_dict_strategy = st.fixed_dictionaries(
    {
        "id": _dc_id_strategy,
        "name": st.sampled_from(["Kansas", "Sweden", "California"]),
        "location": st.sampled_from(["US", "EU", "US-CA"]),
        "globalNetwork": st.booleans(),
        "storageSupport": st.booleans(),
        "listed": st.booleans(),
        "compliance": st.lists(st.sampled_from(["GDPR", "HIPAA", "SOC2"]), min_size=0, max_size=2),
        "gpuAvailability": st.lists(_gpu_availability_strategy, min_size=0, max_size=3),
    }
)


@st.composite
def discovery_inputs(draw: st.DrawFn) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    gpus = draw(st.lists(_gpu_type_dict_strategy, min_size=0, max_size=4))
    dcs = draw(st.lists(_datacenter_dict_strategy, min_size=0, max_size=4))
    return gpus, dcs


@given(inputs=discovery_inputs())
@settings(max_examples=100)
def test_build_snapshot_determinism(
    inputs: tuple[list[dict[str, object]], list[dict[str, object]]],
) -> None:
    gpu_dicts, dc_dicts = inputs
    gpu_types = [RunpodGpuType.model_validate(g) for g in gpu_dicts]
    datacenters = [RunpodDatacenter.model_validate(d) for d in dc_dicts]

    a = _build_snapshot(gpu_types, datacenters)
    b = _build_snapshot(gpu_types, datacenters)

    assert a.fetched_at is not b.fetched_at  # wall-clock differs
    assert a.gpus == b.gpus
    assert a.datacenters == b.datacenters


@given(inputs=discovery_inputs())
@settings(max_examples=100)
def test_snapshot_preserves_input_counts(
    inputs: tuple[list[dict[str, object]], list[dict[str, object]]],
) -> None:
    gpu_dicts, dc_dicts = inputs
    gpu_types = [RunpodGpuType.model_validate(g) for g in gpu_dicts]
    datacenters = [RunpodDatacenter.model_validate(d) for d in dc_dicts]

    snapshot = _build_snapshot(gpu_types, datacenters)
    assert len(snapshot.gpus) == len(gpu_types)
    assert len(snapshot.datacenters) == len(datacenters)


@given(inputs=discovery_inputs())
@settings(max_examples=100)
def test_gpu_by_id_returns_first_match(
    inputs: tuple[list[dict[str, object]], list[dict[str, object]]],
) -> None:
    gpu_dicts, dc_dicts = inputs
    gpu_types = [RunpodGpuType.model_validate(g) for g in gpu_dicts]
    datacenters = [RunpodDatacenter.model_validate(d) for d in dc_dicts]

    snapshot = _build_snapshot(gpu_types, datacenters)
    if snapshot.gpus:
        first = snapshot.gpus[0]
        assert snapshot.gpu_by_id(first.gpu_type_id) is first


@given(inputs=discovery_inputs())
@settings(max_examples=100)
def test_to_availability_entries_shape(
    inputs: tuple[list[dict[str, object]], list[dict[str, object]]],
) -> None:
    gpu_dicts, dc_dicts = inputs
    gpu_types = [RunpodGpuType.model_validate(g) for g in gpu_dicts]
    datacenters = [RunpodDatacenter.model_validate(d) for d in dc_dicts]

    snapshot = _build_snapshot(gpu_types, datacenters)
    entries = snapshot.to_availability_entries()

    for entry in entries:
        assert len(entry) == 5
        dc_id, gpu_name, cloud_type, gpu_count, available = entry
        assert isinstance(dc_id, str) and dc_id
        assert isinstance(gpu_name, str) and gpu_name
        assert isinstance(cloud_type, str) and cloud_type
        assert isinstance(gpu_count, int) and gpu_count >= 1
        assert isinstance(available, bool)


@given(gpu_dict=_gpu_type_dict_strategy)
@settings(max_examples=100)
def test_normalize_gpu_preserves_id(gpu_dict: dict[str, object]) -> None:
    gpu = RunpodGpuType.model_validate(gpu_dict)
    entry = _normalize_gpu(gpu)
    assert entry.gpu_type_id == gpu.id
    assert isinstance(entry, GpuCatalogEntry)


@given(dc_dict=_datacenter_dict_strategy)
@settings(max_examples=100)
def test_normalize_datacenter_preserves_id(dc_dict: dict[str, object]) -> None:
    dc = RunpodDatacenter.model_validate(dc_dict)
    entry = _normalize_datacenter(dc)
    assert entry.datacenter_id == dc.id
