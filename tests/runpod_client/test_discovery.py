"""Hermetic tests for the GPU discovery service.

All network I/O is mocked via ``httpx.MockTransport``; no live RunPod calls.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import httpx
import pytest

from pitwall.routing.context import AvailabilitySnapshot
from pitwall.runpod_client.discovery import (
    DEFAULT_DISCOVERY_TTL_S,
    GpuCatalogEntry,
    GpuDiscoveryService,
    GpuDiscoverySnapshot,
)
from pitwall.runpod_client.graphql import (
    RunpodGraphQLClient,
    RunpodGraphQLError,
)

pytestmark = pytest.mark.anyio


def _graphql_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={"Content-Type": "application/json"},
    )


class _FakeGraphQL:
    def __init__(self) -> None:
        self.responses: list[httpx.Response] = []
        self.requests: list[httpx.Request] = []

    def add(self, response: httpx.Response) -> None:
        self.responses.append(response)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected GraphQL request: {request.method} {request.url}")
        response = self.responses.pop(0)
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def fake_graphql() -> _FakeGraphQL:
    return _FakeGraphQL()


@pytest.fixture
def discovery_service(fake_graphql: _FakeGraphQL) -> GpuDiscoveryService:
    client = RunpodGraphQLClient(api_key="test-key", transport=fake_graphql.transport())
    return GpuDiscoveryService(client, ttl_s=1.0)


_GPU_TYPES_RESPONSE = """
{
  "data": {
    "gpuTypes": [
      {
        "id": "NVIDIA L4",
        "displayName": "NVIDIA L4",
        "manufacturer": "NVIDIA",
        "memoryInGb": 24,
        "cudaCores": 7424,
        "secureCloud": true,
        "communityCloud": true,
        "securePrice": 0.44,
        "communityPrice": 0.34,
        "secureSpotPrice": 0.27,
        "communitySpotPrice": 0.18,
        "maxGpuCount": 8,
        "lowestPrice": {
          "gpuTypeId": "NVIDIA L4",
          "minimumBidPrice": 0.19,
          "uninterruptablePrice": 0.44,
          "stockStatus": "High",
          "availableGpuCounts": [1, 2, 4]
        },
        "nodeGroupDatacenters": [
          {"id": "US-KS-2", "name": "Kansas", "location": "US"},
          {"id": "EU-SE-1", "name": "Sweden", "location": "Stockholm"}
        ]
      },
      {
        "id": "NVIDIA H100 80GB HBM3",
        "displayName": "NVIDIA H100 80GB HBM3",
        "manufacturer": "NVIDIA",
        "memoryInGb": 80,
        "cudaCores": 16896,
        "secureCloud": true,
        "communityCloud": false,
        "securePrice": 2.99,
        "communityPrice": null,
        "secureSpotPrice": 1.89,
        "communitySpotPrice": null,
        "maxGpuCount": 2,
        "lowestPrice": {
          "gpuTypeId": "NVIDIA H100 80GB HBM3",
          "minimumBidPrice": 1.75,
          "uninterruptablePrice": 2.99,
          "stockStatus": "Low",
          "availableGpuCounts": [1]
        },
        "nodeGroupDatacenters": [
          {"id": "EU-SE-1", "name": "Sweden", "location": "Stockholm"}
        ]
      }
    ]
  }
}
"""

_DATACENTERS_RESPONSE = """
{
  "data": {
    "myself": {
      "datacenters": [
        {
          "id": "US-KS-2",
          "name": "Kansas",
          "location": "US",
          "globalNetwork": false,
          "storageSupport": true,
          "listed": true,
          "compliance": [],
          "gpuAvailability": [
            {
              "available": true,
              "stockStatus": "Available",
              "gpuTypeId": "NVIDIA L4",
              "gpuTypeDisplayName": "NVIDIA L4",
              "displayName": "L4 lane",
              "id": "lane-1"
            }
          ]
        },
        {
          "id": "EU-SE-1",
          "name": "Sweden",
          "location": "Stockholm",
          "globalNetwork": true,
          "storageSupport": true,
          "listed": true,
          "compliance": ["GDPR"],
          "gpuAvailability": [
            {
              "available": true,
              "stockStatus": "Available",
              "gpuTypeId": "NVIDIA L4",
              "gpuTypeDisplayName": "NVIDIA L4",
              "displayName": "L4 lane",
              "id": "lane-2"
            },
            {
              "available": false,
              "stockStatus": "Out of Stock",
              "gpuTypeId": "NVIDIA H100 80GB HBM3",
              "gpuTypeDisplayName": "NVIDIA H100 80GB HBM3",
              "displayName": "H100 lane",
              "id": "lane-3"
            }
          ]
        }
      ]
    }
  }
}
"""


class TestDiscoveryServiceHappyPath:
    async def test_refresh_returns_snapshot_with_gpus_and_datacenters(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(_graphql_response(_GPU_TYPES_RESPONSE))
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))

        snapshot = await discovery_service.refresh()
        await discovery_service.aclose()

        assert isinstance(snapshot, GpuDiscoverySnapshot)
        assert snapshot.fetched_at.tzinfo is not None
        assert len(snapshot.gpus) == 2
        assert len(snapshot.datacenters) == 2

    async def test_gpu_catalog_entry_fields(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(_graphql_response(_GPU_TYPES_RESPONSE))
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))

        snapshot = await discovery_service.refresh()
        l4 = snapshot.gpu_by_id("NVIDIA L4")
        assert l4 is not None
        assert l4.display_name == "NVIDIA L4"
        assert l4.manufacturer == "NVIDIA"
        assert l4.memory_in_gb == 24
        assert l4.cuda_cores == 7424
        assert l4.secure_cloud is True
        assert l4.community_cloud is True
        assert l4.secure_price == Decimal("0.44")
        assert l4.community_price == Decimal("0.34")
        assert l4.secure_spot_price == Decimal("0.27")
        assert l4.community_spot_price == Decimal("0.18")
        assert l4.lowest_bid_price == Decimal("0.19")
        assert l4.uninterruptable_price == Decimal("0.44")
        assert l4.datacenter_ids == ("EU-SE-1", "US-KS-2")
        assert l4.available_gpu_counts == (1, 2, 4)
        assert l4.stock_status == "High"
        assert l4.max_gpu_count == 8

    async def test_datacenter_catalog_entry_fields(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(_graphql_response(_GPU_TYPES_RESPONSE))
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))

        snapshot = await discovery_service.refresh()
        dc = snapshot.datacenter_by_id("EU-SE-1")
        assert dc is not None
        assert dc.name == "Sweden"
        assert dc.location == "Stockholm"
        assert dc.global_network is True
        assert dc.storage_support is True
        assert dc.listed is True
        assert dc.compliance == ("GDPR",)
        assert dc.gpu_types == ("NVIDIA H100 80GB HBM3", "NVIDIA L4")
        assert dc.gpu_availability == {
            "NVIDIA L4": True,
            "NVIDIA H100 80GB HBM3": False,
        }

    async def test_get_snapshot_uses_cache_when_fresh(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(_graphql_response(_GPU_TYPES_RESPONSE))
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))

        first = await discovery_service.get_snapshot()
        second = await discovery_service.get_snapshot()
        await discovery_service.aclose()

        assert first is second
        assert len(fake_graphql.requests) == 2  # one gpu_types + one datacenters

    async def test_get_snapshot_refreshes_when_stale(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(_graphql_response(_GPU_TYPES_RESPONSE))
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))
        fake_graphql.add(_graphql_response(_GPU_TYPES_RESPONSE))
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))

        await discovery_service.get_snapshot()
        discovery_service.invalidate()
        second = await discovery_service.get_snapshot()
        await discovery_service.aclose()

        assert len(fake_graphql.requests) == 4
        assert second is not None

    async def test_get_gpu_and_get_datacenter_after_refresh(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(_graphql_response(_GPU_TYPES_RESPONSE))
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))

        await discovery_service.refresh()
        gpu = discovery_service.get_gpu("NVIDIA L4")
        dc = discovery_service.get_datacenter("US-KS-2")
        await discovery_service.aclose()

        assert gpu is not None
        assert gpu.gpu_type_id == "NVIDIA L4"
        assert dc is not None
        assert dc.datacenter_id == "US-KS-2"

    async def test_get_gpu_returns_none_before_refresh(
        self,
        discovery_service: GpuDiscoveryService,
    ) -> None:
        assert discovery_service.get_gpu("NVIDIA L4") is None
        await discovery_service.aclose()


class TestDiscoverySnapshotHelpers:
    async def test_to_availability_entries_default_clouds(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(_graphql_response(_GPU_TYPES_RESPONSE))
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))

        snapshot = await discovery_service.refresh()
        entries = snapshot.to_availability_entries(gpu_count=1)
        await discovery_service.aclose()

        # L4 is in both datacenters, secure+community
        # H100 is only in EU-SE-1, secure only
        assert len(entries) > 0
        keys = {(e[0], e[1], e[2], e[3]) for e in entries}
        assert ("US-KS-2", "NVIDIA L4", "SECURE", 1) in keys
        assert ("US-KS-2", "NVIDIA L4", "COMMUNITY", 1) in keys
        assert ("EU-SE-1", "NVIDIA L4", "SECURE", 1) in keys
        assert ("EU-SE-1", "NVIDIA H100 80GB HBM3", "SECURE", 1) in keys

    async def test_to_availability_entries_with_cloud_override(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(_graphql_response(_GPU_TYPES_RESPONSE))
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))

        snapshot = await discovery_service.refresh()
        entries = snapshot.to_availability_entries(gpu_count=1, cloud_type="SECURE")
        await discovery_service.aclose()

        for entry in entries:
            assert entry[2] == "SECURE"

    async def test_to_availability_snapshot(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(_graphql_response(_GPU_TYPES_RESPONSE))
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))

        snapshot = await discovery_service.refresh()
        avail = snapshot.to_availability_snapshot(gpu_count=1, cloud_type="SECURE")
        await discovery_service.aclose()

        assert isinstance(avail, AvailabilitySnapshot)
        assert avail.is_available("US-KS-2", "NVIDIA L4", "SECURE", 1) is True
        assert avail.is_available("EU-SE-1", "NVIDIA H100 80GB HBM3", "SECURE", 1) is False


class TestDiscoveryServiceErrors:
    async def test_graphql_error_propagates(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(
            _graphql_response('{"errors": [{"message": "unauthorized"}], "data": null}')
        )

        with pytest.raises(RunpodGraphQLError):
            await discovery_service.refresh()
        await discovery_service.aclose()

    async def test_empty_gpu_types_returns_empty_snapshot(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(_graphql_response('{"data": {"gpuTypes": []}}'))
        fake_graphql.add(_graphql_response('{"data": {"myself": {"datacenters": []}}}'))

        snapshot = await discovery_service.refresh()
        await discovery_service.aclose()

        assert snapshot.gpus == ()
        assert snapshot.datacenters == ()


class TestDiscoveryServiceEdgeCases:
    async def test_gpu_without_datacenters(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(
            _graphql_response('{"data": {"gpuTypes": [{"id": "NVIDIA L4", "secureCloud": true}]}}')
        )
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))

        snapshot = await discovery_service.refresh()
        gpu = snapshot.gpu_by_id("NVIDIA L4")
        await discovery_service.aclose()

        assert gpu is not None
        assert gpu.datacenter_ids == ()

    async def test_datacenter_without_gpu_availability(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(_graphql_response(_GPU_TYPES_RESPONSE))
        fake_graphql.add(
            _graphql_response(
                '{"data": {"myself": {"datacenters": [{"id": "US-KS-2", "name": "Kansas"}]}}}'
            )
        )

        snapshot = await discovery_service.refresh()
        dc = snapshot.datacenter_by_id("US-KS-2")
        await discovery_service.aclose()

        assert dc is not None
        assert dc.gpu_types == ()
        assert dc.gpu_availability == {}

    async def test_null_lowest_price_fields(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(
            _graphql_response(
                '{"data": {"gpuTypes": [{"id": "NVIDIA L4", "secureCloud": true, "lowestPrice": null}]}}'
            )
        )
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))

        snapshot = await discovery_service.refresh()
        gpu = snapshot.gpu_by_id("NVIDIA L4")
        await discovery_service.aclose()

        assert gpu is not None
        assert gpu.lowest_bid_price is None
        assert gpu.available_gpu_counts == ()

    async def test_concurrent_refresh_is_serialized(
        self,
        discovery_service: GpuDiscoveryService,
        fake_graphql: _FakeGraphQL,
    ) -> None:
        fake_graphql.add(_graphql_response(_GPU_TYPES_RESPONSE))
        fake_graphql.add(_graphql_response(_DATACENTERS_RESPONSE))

        async def fetch() -> GpuDiscoverySnapshot:
            return await discovery_service.get_snapshot()

        results = await asyncio.gather(fetch(), fetch(), fetch())
        await discovery_service.aclose()

        # All three calls should share the same snapshot object.
        assert results[0] is results[1] is results[2]
        # Only one GraphQL round-trip (gpu_types + datacenters).
        assert len(fake_graphql.requests) == 2


class TestDiscoveryServiceValidation:
    def test_negative_ttl_raises(self, fake_graphql: _FakeGraphQL) -> None:
        client = RunpodGraphQLClient(api_key="test-key", transport=fake_graphql.transport())
        with pytest.raises(ValueError, match="ttl_s must be non-negative"):
            GpuDiscoveryService(client, ttl_s=-1.0)

    def test_zero_ttl_always_stale(self, fake_graphql: _FakeGraphQL) -> None:
        client = RunpodGraphQLClient(api_key="test-key", transport=fake_graphql.transport())
        svc = GpuDiscoveryService(client, ttl_s=0.0)
        assert svc._is_stale() is True


class TestGpuDiscoverySnapshotImmutability:
    def test_gpu_by_id_missing_returns_none(self) -> None:
        snapshot = GpuDiscoverySnapshot(fetched_at=dt.datetime.now(dt.UTC))
        assert snapshot.gpu_by_id("missing") is None

    def test_datacenter_by_id_missing_returns_none(self) -> None:
        snapshot = GpuDiscoverySnapshot(fetched_at=dt.datetime.now(dt.UTC))
        assert snapshot.datacenter_by_id("missing") is None

    def test_frozen_dataclass_equality(self) -> None:
        a = GpuCatalogEntry(gpu_type_id="NVIDIA L4", secure_cloud=True)
        b = GpuCatalogEntry(gpu_type_id="NVIDIA L4", secure_cloud=True)
        assert a == b
        assert hash(a) == hash(b)

    def test_frozen_dataclass_inequality(self) -> None:
        a = GpuCatalogEntry(gpu_type_id="NVIDIA L4")
        b = GpuCatalogEntry(gpu_type_id="NVIDIA H100")
        assert a != b


class TestDefaultConstants:
    def test_default_ttl(self) -> None:
        assert DEFAULT_DISCOVERY_TTL_S == 60.0
