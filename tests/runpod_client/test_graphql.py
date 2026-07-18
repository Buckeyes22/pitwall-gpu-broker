from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pytest

from pitwall.runpod_client.graphql import (
    RunpodGraphQLClient,
    RunpodGraphQLError,
)

pytestmark = pytest.mark.anyio

GraphQLHandler = Callable[[httpx.Request], httpx.Response]


def _graphql_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={"Content-Type": "application/json"},
    )


@dataclass
class RunpodGraphQLFake:
    responses: list[httpx.Response | GraphQLHandler] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def add(self, response: httpx.Response | GraphQLHandler) -> None:
        self.responses.append(response)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected GraphQL request: {request.method} {request.url}")
        response = self.responses.pop(0)
        if callable(response):
            return response(request)
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
            extensions=response.extensions,
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _request_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


async def test_gpu_types_returns_live_prices_as_decimals() -> None:
    fake = RunpodGraphQLFake()
    fake.add(
        _graphql_response(
            """
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
                    "securePrice": 0.440000000000000001,
                    "communityPrice": 0.340000000000000001,
                    "secureSpotPrice": 0.270000000000000001,
                    "communitySpotPrice": 0.180000000000000001,
                    "lowestPrice": {
                      "gpuTypeId": "NVIDIA L4",
                      "minimumBidPrice": 0.190000000000000001,
                      "uninterruptablePrice": 0.440000000000000001,
                      "stockStatus": "High"
                    },
                    "nodeGroupDatacenters": [
                      {"id": "US-KS-2", "name": "Kansas", "location": "US"}
                    ]
                  }
                ]
              }
            }
            """
        )
    )
    client = RunpodGraphQLClient(api_key="test-key", transport=fake.transport())

    gpu_types = await client.gpu_types()
    await client.aclose()

    assert len(gpu_types) == 1
    gpu = gpu_types[0]
    assert gpu.id == "NVIDIA L4"
    assert gpu.memory_in_gb == 24
    assert gpu.secure_cloud
    assert gpu.community_cloud
    assert gpu.secure_price == Decimal("0.440000000000000001")
    assert gpu.community_price == Decimal("0.340000000000000001")
    assert gpu.secure_spot_price == Decimal("0.270000000000000001")
    assert gpu.community_spot_price == Decimal("0.180000000000000001")
    assert gpu.lowest_price is not None
    assert gpu.lowest_price.minimum_bid_price == Decimal("0.190000000000000001")
    assert gpu.node_group_datacenters[0].id == "US-KS-2"


async def test_datacenters_returns_gpu_availability_by_datacenter() -> None:
    fake = RunpodGraphQLFake()
    fake.add(
        _graphql_response(
            """
            {
              "data": {
                "myself": {
                  "datacenters": [
                    {
                      "id": "EU-SE-1",
                      "name": "Sweden",
                      "location": "Stockholm",
                      "globalNetwork": true,
                      "storageSupport": true,
                      "listed": true,
                      "gpuAvailability": [
                        {
                          "available": true,
                          "stockStatus": "Available",
                          "gpuTypeId": "NVIDIA H100 80GB HBM3",
                          "gpuTypeDisplayName": "NVIDIA H100 80GB HBM3",
                          "displayName": "H100 lane",
                          "id": "lane-1"
                        }
                      ],
                      "compliance": ["GDPR"]
                    }
                  ]
                }
              }
            }
            """
        )
    )
    client = RunpodGraphQLClient(api_key="test-key", transport=fake.transport())

    datacenters = await client.datacenters()
    await client.aclose()

    assert datacenters[0].id == "EU-SE-1"
    assert datacenters[0].location == "Stockholm"
    assert datacenters[0].global_network
    assert datacenters[0].gpu_availability[0].available
    assert datacenters[0].gpu_availability[0].gpu_type_id == "NVIDIA H100 80GB HBM3"
    assert datacenters[0].compliance == ["GDPR"]


async def test_get_bid_price_queries_lowest_price_for_gpu_and_datacenter() -> None:
    fake = RunpodGraphQLFake()
    fake.add(
        _graphql_response(
            """
            {
              "data": {
                "gpuTypes": [
                  {
                    "id": "NVIDIA A100 80GB",
                    "displayName": "NVIDIA A100 80GB",
                    "secureSpotPrice": 0.720000,
                    "communitySpotPrice": 0.610000,
                    "lowestPrice": {
                      "gpuTypeId": "NVIDIA A100 80GB",
                      "gpuName": "NVIDIA A100 80GB",
                      "minimumBidPrice": 0.625000,
                      "uninterruptablePrice": 1.890000,
                      "stockStatus": "Low",
                      "availableGpuCounts": [1, 2]
                    }
                  }
                ]
              }
            }
            """
        )
    )
    client = RunpodGraphQLClient(api_key="test-key", transport=fake.transport())

    bid = await client.get_bid_price(
        "NVIDIA A100 80GB",
        data_center_id="EU-SE-1",
        secure_cloud=True,
        gpu_count=2,
    )
    await client.aclose()

    body = _request_body(fake.requests[0])
    assert body["variables"] == {
        "input": {"id": "NVIDIA A100 80GB"},
        "priceInput": {
            "dataCenterId": "EU-SE-1",
            "globalNetwork": False,
            "gpuCount": 2,
            "secureCloud": True,
        },
    }
    assert bid.gpu_type_id == "NVIDIA A100 80GB"
    assert bid.minimum_bid_price == Decimal("0.625000")
    assert bid.uninterruptable_price == Decimal("1.890000")
    assert bid.available_gpu_counts == [1, 2]


async def test_set_bid_price_uses_decimal_literal_without_float_payload() -> None:
    fake = RunpodGraphQLFake()

    def handler(request: httpx.Request) -> httpx.Response:
        body = _request_body(request)
        assert "bidPerGpu: 0.333333333333333333" in body["query"]
        assert body["variables"] == {"podId": "pod-1", "gpuCount": 1}
        assert "0.333333333333333333" not in json.dumps(body["variables"])
        return _graphql_response(
            """
            {
              "data": {
                "podBidResume": {
                  "id": "pod-1",
                  "desiredStatus": "RUNNING",
                  "gpuCount": 1,
                  "costPerHr": 0.333333333333333333,
                  "lowestBidPriceToResume": 0.310000000000000000
                }
              }
            }
            """
        )

    fake.add(handler)
    client = RunpodGraphQLClient(api_key="test-key", transport=fake.transport())

    result = await client.set_bid_price(
        pod_id="pod-1",
        bid_per_gpu=Decimal("0.333333333333333333"),
    )
    await client.aclose()

    assert result.pod_id == "pod-1"
    assert result.cost_per_hr == Decimal("0.333333333333333333")
    assert result.lowest_bid_price_to_resume == Decimal("0.310000000000000000")


async def test_credits_balance_reads_client_balance_and_spend() -> None:
    fake = RunpodGraphQLFake()
    fake.add(
        _graphql_response(
            """
            {
              "data": {
                "myself": {
                  "id": "user-1",
                  "clientBalance": 42.250000000000000001,
                  "currentSpendPerHr": 1.125000000000000001,
                  "spendLimit": 100.000000000000000001,
                  "minBalance": 5.000000000000000001,
                  "underBalance": false
                }
              }
            }
            """
        )
    )
    client = RunpodGraphQLClient(api_key="test-key", transport=fake.transport())

    balance = await client.credits_balance()
    await client.aclose()

    assert balance.user_id == "user-1"
    assert balance.client_balance == Decimal("42.250000000000000001")
    assert balance.current_spend_per_hr == Decimal("1.125000000000000001")
    assert balance.spend_limit == Decimal("100.000000000000000001")
    assert balance.min_balance == Decimal("5.000000000000000001")
    assert not balance.under_balance


async def test_graphql_error_envelope_raises_typed_client_error() -> None:
    fake = RunpodGraphQLFake()
    fake.add(
        _graphql_response(
            """
            {
              "errors": [
                {"message": "unauthorized", "path": ["myself"]},
                {"message": "token expired"}
              ],
              "data": {"myself": null}
            }
            """
        )
    )
    client = RunpodGraphQLClient(api_key="bad-key", transport=fake.transport())

    with pytest.raises(RunpodGraphQLError) as exc_info:
        await client.credits_balance()
    await client.aclose()

    assert str(exc_info.value) == "RunPod GraphQL errors: unauthorized; token expired"
    assert exc_info.value.errors == [
        {"message": "unauthorized", "path": ["myself"]},
        {"message": "token expired"},
    ]


async def test_from_settings_uses_runpod_api_key_in_graphql_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = RunpodGraphQLFake()
    fake.add(_graphql_response('{"data": {"myself": {"clientBalance": 1.25}}}'))
    monkeypatch.setenv("RUNPOD_API_KEY", "settings-key-123")
    client = RunpodGraphQLClient.from_settings(transport=fake.transport())

    await client.credits_balance()
    await client.aclose()

    # Auth travels in the Authorization header, NOT the URL (no secret in URLs).
    assert fake.requests[0].headers["authorization"] == "Bearer settings-key-123"
    assert b"api_key" not in fake.requests[0].url.query
    assert fake.requests[0].headers["content-type"] == "application/json"
