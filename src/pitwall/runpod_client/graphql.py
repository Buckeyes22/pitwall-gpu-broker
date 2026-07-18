"""RunPod GraphQL client for live GPU market, datacenter, and billing reads."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Self, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pitwall.config import PitwallSettings, load_settings_from_env
from pitwall.runpod_client.pods import RunPodError

RUNPOD_GRAPHQL_URL = "https://api.runpod.io/graphql"

JsonObject = dict[str, Any]


class RunpodGraphQLError(RunPodError):
    """RunPod returned a GraphQL ``errors`` envelope."""

    def __init__(self, errors: list[JsonObject]) -> None:
        self.errors = errors
        messages = [_graphql_error_message(error) for error in errors]
        super().__init__(f"RunPod GraphQL errors: {'; '.join(messages)}")


class RunpodGraphQLHTTPError(RunPodError):
    """RunPod GraphQL endpoint returned a non-2xx HTTP status."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"RunPod GraphQL HTTP {status_code}: {body}")


class RunpodGraphQLResponseError(RunPodError):
    """RunPod GraphQL response body did not match the expected shape."""


class _GraphQLModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


def _coerce_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"expected decimal-compatible value, got {value!r}") from exc


class RunpodLowestPrice(_GraphQLModel):
    """Lowest live price metadata for a GPU query lane."""

    gpu_name: str | None = Field(default=None, alias="gpuName")
    gpu_type_id: str | None = Field(default=None, alias="gpuTypeId")
    minimum_bid_price: Decimal | None = Field(default=None, alias="minimumBidPrice")
    uninterruptable_price: Decimal | None = Field(
        default=None,
        alias="uninterruptablePrice",
    )
    min_memory: int | None = Field(default=None, alias="minMemory")
    min_vcpu: int | None = Field(default=None, alias="minVcpu")
    stock_status: str | None = Field(default=None, alias="stockStatus")
    country_code: str | None = Field(default=None, alias="countryCode")
    support_public_ip: bool | None = Field(default=None, alias="supportPublicIp")
    max_gpu_count: int | None = Field(default=None, alias="maxGpuCount")
    available_gpu_counts: list[int] = Field(default_factory=list, alias="availableGpuCounts")

    @field_validator("minimum_bid_price", "uninterruptable_price", mode="before")
    @classmethod
    def _validate_money(cls, value: object) -> Decimal | None:
        return _coerce_decimal(value)


class RunpodGpuAvailability(_GraphQLModel):
    """GPU lane availability within one RunPod datacenter."""

    available: bool | None = None
    stock_status: str | None = Field(default=None, alias="stockStatus")
    gpu_type_id: str | None = Field(default=None, alias="gpuTypeId")
    gpu_type_display_name: str | None = Field(default=None, alias="gpuTypeDisplayName")
    display_name: str | None = Field(default=None, alias="displayName")
    id: str | None = None


class RunpodDatacenter(_GraphQLModel):
    """RunPod datacenter plus the GPU types available there."""

    id: str
    name: str | None = None
    location: str | None = None
    global_network: bool = Field(default=False, alias="globalNetwork")
    storage_support: bool = Field(default=False, alias="storageSupport")
    listed: bool = False
    gpu_availability: list[RunpodGpuAvailability] = Field(
        default_factory=list,
        alias="gpuAvailability",
    )
    compliance: list[str] = Field(default_factory=list)


class RunpodGpuType(_GraphQLModel):
    """RunPod GPU type with live on-demand and spot prices."""

    id: str
    display_name: str | None = Field(default=None, alias="displayName")
    manufacturer: str | None = None
    memory_in_gb: int | None = Field(default=None, alias="memoryInGb")
    cuda_cores: int | None = Field(default=None, alias="cudaCores")
    secure_cloud: bool = Field(default=False, alias="secureCloud")
    community_cloud: bool = Field(default=False, alias="communityCloud")
    secure_price: Decimal | None = Field(default=None, alias="securePrice")
    cluster_price: Decimal | None = Field(default=None, alias="clusterPrice")
    community_price: Decimal | None = Field(default=None, alias="communityPrice")
    one_month_price: Decimal | None = Field(default=None, alias="oneMonthPrice")
    three_month_price: Decimal | None = Field(default=None, alias="threeMonthPrice")
    six_month_price: Decimal | None = Field(default=None, alias="sixMonthPrice")
    one_week_price: Decimal | None = Field(default=None, alias="oneWeekPrice")
    secure_spot_price: Decimal | None = Field(default=None, alias="secureSpotPrice")
    community_spot_price: Decimal | None = Field(default=None, alias="communitySpotPrice")
    throughput: int | None = None
    max_gpu_count: int | None = Field(default=None, alias="maxGpuCount")
    max_gpu_count_community_cloud: int | None = Field(
        default=None,
        alias="maxGpuCountCommunityCloud",
    )
    max_gpu_count_secure_cloud: int | None = Field(
        default=None,
        alias="maxGpuCountSecureCloud",
    )
    min_pod_gpu_count: int | None = Field(default=None, alias="minPodGpuCount")
    node_group_gpu_sizes: list[int] = Field(default_factory=list, alias="nodeGroupGpuSizes")
    node_group_datacenters: list[RunpodDatacenter] = Field(
        default_factory=list,
        alias="nodeGroupDatacenters",
    )
    lowest_price: RunpodLowestPrice | None = Field(default=None, alias="lowestPrice")

    @field_validator(
        "secure_price",
        "cluster_price",
        "community_price",
        "one_month_price",
        "three_month_price",
        "six_month_price",
        "one_week_price",
        "secure_spot_price",
        "community_spot_price",
        mode="before",
    )
    @classmethod
    def _validate_money(cls, value: object) -> Decimal | None:
        return _coerce_decimal(value)


class RunpodBidPrice(_GraphQLModel):
    """Current minimum spot bid and adjacent live price context."""

    gpu_type_id: str
    gpu_name: str | None = None
    data_center_id: str | None = None
    gpu_count: int
    secure_cloud: bool | None = None
    minimum_bid_price: Decimal | None = None
    uninterruptable_price: Decimal | None = None
    secure_spot_price: Decimal | None = None
    community_spot_price: Decimal | None = None
    stock_status: str | None = None
    available_gpu_counts: list[int] = Field(default_factory=list)

    @field_validator(
        "minimum_bid_price",
        "uninterruptable_price",
        "secure_spot_price",
        "community_spot_price",
        mode="before",
    )
    @classmethod
    def _validate_money(cls, value: object) -> Decimal | None:
        return _coerce_decimal(value)


class RunpodBidResumeResult(_GraphQLModel):
    """Result returned after applying a spot resume bid to a pod."""

    pod_id: str = Field(alias="id")
    desired_status: str | None = Field(default=None, alias="desiredStatus")
    gpu_count: int | None = Field(default=None, alias="gpuCount")
    cost_per_hr: Decimal | None = Field(default=None, alias="costPerHr")
    lowest_bid_price_to_resume: Decimal | None = Field(
        default=None,
        alias="lowestBidPriceToResume",
    )

    @field_validator("cost_per_hr", "lowest_bid_price_to_resume", mode="before")
    @classmethod
    def _validate_money(cls, value: object) -> Decimal | None:
        return _coerce_decimal(value)


class RunpodCreditsBalance(_GraphQLModel):
    """RunPod client credit balance and spend guardrail fields."""

    user_id: str | None = Field(default=None, alias="id")
    client_balance: Decimal = Field(default=Decimal("0"), alias="clientBalance")
    current_spend_per_hr: Decimal | None = Field(default=None, alias="currentSpendPerHr")
    spend_limit: Decimal | None = Field(default=None, alias="spendLimit")
    min_balance: Decimal | None = Field(default=None, alias="minBalance")
    under_balance: bool = Field(default=False, alias="underBalance")

    @field_validator(
        "client_balance",
        "current_spend_per_hr",
        "spend_limit",
        "min_balance",
        mode="before",
    )
    @classmethod
    def _validate_money(cls, value: object) -> Decimal | None:
        return _coerce_decimal(value)


GPU_TYPES_QUERY = """
query pitwallGpuTypes($input: GpuTypeFilter) {
  gpuTypes(input: $input) {
    id
    displayName
    manufacturer
    memoryInGb
    cudaCores
    secureCloud
    communityCloud
    securePrice
    clusterPrice
    communityPrice
    oneMonthPrice
    threeMonthPrice
    sixMonthPrice
    oneWeekPrice
    communitySpotPrice
    secureSpotPrice
    throughput
    maxGpuCount
    maxGpuCountCommunityCloud
    maxGpuCountSecureCloud
    minPodGpuCount
    nodeGroupGpuSizes
    lowestPrice(input: {gpuCount: 1, globalNetwork: false}) {
      gpuName
      gpuTypeId
      minimumBidPrice
      uninterruptablePrice
      minMemory
      minVcpu
      stockStatus
      countryCode
      supportPublicIp
      maxGpuCount
      availableGpuCounts
    }
    nodeGroupDatacenters {
      id
      name
      location
      globalNetwork
      storageSupport
      listed
      compliance
      gpuAvailability {
        available
        stockStatus
        gpuTypeId
        gpuTypeDisplayName
        displayName
        id
      }
    }
  }
}
"""

DATACENTERS_QUERY = """
query pitwallDatacenters {
  myself {
    datacenters {
      id
      name
      location
      globalNetwork
      storageSupport
      listed
      compliance
      gpuAvailability {
        available
        stockStatus
        gpuTypeId
        gpuTypeDisplayName
        displayName
        id
      }
    }
  }
}
"""

BID_PRICE_QUERY = """
query pitwallBidPrice($input: GpuTypeFilter, $priceInput: GpuLowestPriceInput) {
  gpuTypes(input: $input) {
    id
    displayName
    secureSpotPrice
    communitySpotPrice
    lowestPrice(input: $priceInput) {
      gpuName
      gpuTypeId
      minimumBidPrice
      uninterruptablePrice
      stockStatus
      maxGpuCount
      availableGpuCounts
    }
  }
}
"""

CREDITS_BALANCE_QUERY = """
query pitwallCreditsBalance {
  myself {
    id
    clientBalance
    currentSpendPerHr
    spendLimit
    minBalance
    underBalance
  }
}
"""


class RunpodGraphQLClient:
    """Async httpx-backed client for RunPod's GraphQL-only broker surfaces."""

    def __init__(
        self,
        *,
        api_key: str,
        graphql_url: str = RUNPOD_GRAPHQL_URL,
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise RunPodError("RUNPOD_API_KEY not set in settings")
        self._api_key = api_key
        # Authenticate via the Authorization: Bearer header only (set below) — the
        # same mechanism the REST clients use. Do NOT put the api_key in the URL
        # query string: secrets in URLs leak through logs, proxies, and error reprs.
        self._graphql_url = graphql_url
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            transport=transport,
        )

    @classmethod
    def from_settings(
        cls,
        settings: PitwallSettings | None = None,
        *,
        graphql_url: str = RUNPOD_GRAPHQL_URL,
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> Self:
        resolved_settings = settings or load_settings_from_env()
        return cls(
            api_key=resolved_settings.runpod_api_key,
            graphql_url=graphql_url,
            timeout_s=timeout_s,
            transport=transport,
        )

    async def gpu_types(self) -> list[RunpodGpuType]:
        """Return RunPod GPU types with live on-demand, spot, and datacenter data."""
        data = await self._graphql(GPU_TYPES_QUERY, variables={"input": None})
        return _model_list(data.get("gpuTypes"), RunpodGpuType)

    async def datacenters(self) -> list[RunpodDatacenter]:
        """Return datacenters and the GPU lanes currently available in each."""
        data = await self._graphql(DATACENTERS_QUERY)
        myself = _object_field(data, "myself")
        return _model_list(myself.get("datacenters"), RunpodDatacenter)

    async def get_bid_price(
        self,
        gpu_type_id: str,
        dc: str | None = None,
        *,
        data_center_id: str | None = None,
        secure_cloud: bool | None = None,
        gpu_count: int = 1,
    ) -> RunpodBidPrice:
        """Return the current minimum spot bid for a GPU type and optional datacenter."""
        if gpu_count < 1:
            raise ValueError("gpu_count must be >= 1")
        resolved_data_center_id = _resolve_data_center_id(dc, data_center_id)
        price_input: JsonObject = {
            "gpuCount": gpu_count,
            "globalNetwork": False,
        }
        if resolved_data_center_id is not None:
            price_input["dataCenterId"] = resolved_data_center_id
        if secure_cloud is not None:
            price_input["secureCloud"] = secure_cloud
        data = await self._graphql(
            BID_PRICE_QUERY,
            variables={
                "input": {"id": gpu_type_id},
                "priceInput": price_input,
            },
        )
        gpu_types = _model_list(data.get("gpuTypes"), RunpodGpuType)
        if not gpu_types:
            raise RunpodGraphQLResponseError(f"RunPod returned no gpuTypes for {gpu_type_id!r}")
        gpu = gpu_types[0]
        lowest_price = gpu.lowest_price
        resolved_gpu_type_id = gpu.id
        if lowest_price is not None and lowest_price.gpu_type_id is not None:
            resolved_gpu_type_id = lowest_price.gpu_type_id
        return RunpodBidPrice(
            gpu_type_id=resolved_gpu_type_id,
            gpu_name=lowest_price.gpu_name if lowest_price else gpu.display_name,
            data_center_id=resolved_data_center_id,
            gpu_count=gpu_count,
            secure_cloud=secure_cloud,
            minimum_bid_price=lowest_price.minimum_bid_price if lowest_price else None,
            uninterruptable_price=lowest_price.uninterruptable_price if lowest_price else None,
            secure_spot_price=gpu.secure_spot_price,
            community_spot_price=gpu.community_spot_price,
            stock_status=lowest_price.stock_status if lowest_price else None,
            available_gpu_counts=lowest_price.available_gpu_counts if lowest_price else [],
        )

    async def set_bid_price(
        self,
        *,
        pod_id: str,
        bid_per_gpu: Decimal,
        gpu_count: int = 1,
    ) -> RunpodBidResumeResult:
        """Resume an interruptible pod with an exact spot bid per GPU."""
        if not pod_id.strip():
            raise ValueError("pod_id must be non-empty")
        if gpu_count < 1:
            raise ValueError("gpu_count must be >= 1")
        bid_literal = _decimal_graphql_literal(bid_per_gpu)
        query = f"""
mutation pitwallPodBidResume($podId: String!, $gpuCount: Int) {{
  podBidResume(input: {{podId: $podId, gpuCount: $gpuCount, bidPerGpu: {bid_literal}}}) {{
    id
    desiredStatus
    gpuCount
    costPerHr
    lowestBidPriceToResume
  }}
}}
"""
        data = await self._graphql(
            query,
            variables={"podId": pod_id.strip(), "gpuCount": gpu_count},
        )
        return RunpodBidResumeResult.model_validate(_object_field(data, "podBidResume"))

    async def credits_balance(self) -> RunpodCreditsBalance:
        """Return RunPod client credits balance and spend metadata."""
        data = await self._graphql(CREDITS_BALANCE_QUERY)
        return RunpodCreditsBalance.model_validate(_object_field(data, "myself"))

    async def _graphql(
        self,
        query: str,
        *,
        variables: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        payload: JsonObject = {"query": query}
        if variables is not None:
            payload["variables"] = dict(variables)
        response = await self._client.post(
            self._graphql_url,
            content=_graphql_payload_bytes(payload),
        )
        if response.status_code >= 400:
            raise RunpodGraphQLHTTPError(response.status_code, response.text)
        envelope = _decode_json_object(response.content)
        errors = _graphql_errors(envelope)
        if errors:
            raise RunpodGraphQLError(errors)
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise RunpodGraphQLResponseError("RunPod GraphQL response missing data object")
        return cast(JsonObject, data)

    async def aclose(self) -> None:
        await self._client.aclose()


RunPodGraphQLClient = RunpodGraphQLClient


def _graphql_payload_bytes(payload: JsonObject) -> bytes:
    return json.dumps(
        payload,
        default=_json_default,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _decode_json_object(content: bytes) -> JsonObject:
    try:
        parsed = json.loads(content, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise RunpodGraphQLResponseError("RunPod GraphQL response was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise RunpodGraphQLResponseError("RunPod GraphQL response was not a JSON object")
    return cast(JsonObject, parsed)


def _graphql_errors(envelope: Mapping[str, Any]) -> list[JsonObject]:
    raw_errors = envelope.get("errors")
    if not isinstance(raw_errors, list):
        return []
    errors: list[JsonObject] = []
    for raw_error in raw_errors:
        if isinstance(raw_error, dict):
            errors.append(cast(JsonObject, raw_error))
        else:
            errors.append({"message": str(raw_error)})
    return errors


def _graphql_error_message(error: Mapping[str, Any]) -> str:
    message = error.get("message")
    return str(message) if message is not None else str(dict(error))


def _object_field(data: Mapping[str, Any], key: str) -> JsonObject:
    value = data.get(key)
    if not isinstance(value, dict):
        raise RunpodGraphQLResponseError(f"RunPod GraphQL data.{key} was not an object")
    return cast(JsonObject, value)


def _model_list[ModelT: _GraphQLModel](
    raw_value: object,
    model: type[ModelT],
) -> list[ModelT]:
    if not isinstance(raw_value, list):
        raise RunpodGraphQLResponseError("RunPod GraphQL response field was not a list")
    return [model.model_validate(item) for item in raw_value]


def _resolve_data_center_id(dc: str | None, data_center_id: str | None) -> str | None:
    if dc and data_center_id and dc != data_center_id:
        raise ValueError("dc and data_center_id must match when both are provided")
    resolved = data_center_id or dc
    if resolved is None:
        return None
    stripped = resolved.strip()
    return stripped or None


def _decimal_graphql_literal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("bid_per_gpu must be finite")
    if value < 0:
        raise ValueError("bid_per_gpu must be non-negative")
    if value == 0:
        return "0"
    return format(value, "f")


__all__ = [
    "BID_PRICE_QUERY",
    "CREDITS_BALANCE_QUERY",
    "DATACENTERS_QUERY",
    "GPU_TYPES_QUERY",
    "RUNPOD_GRAPHQL_URL",
    "RunPodGraphQLClient",
    "RunpodBidPrice",
    "RunpodBidResumeResult",
    "RunpodCreditsBalance",
    "RunpodDatacenter",
    "RunpodGpuAvailability",
    "RunpodGpuType",
    "RunpodGraphQLError",
    "RunpodGraphQLClient",
    "RunpodGraphQLHTTPError",
    "RunpodGraphQLResponseError",
    "RunpodLowestPrice",
]
