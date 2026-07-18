"""Pydantic v2 schemas for the Provider API surface."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator

from pitwall.core.enums import CapabilitySource, CostMode, ProviderType
from pitwall.core.models import JsonObject, NonNegativeInt, PitwallModel
from pitwall.runpod_client.gpu import (
    validate_canonical_gpu_name,
    validate_canonical_gpu_names,
)

Priority = Annotated[int, Field(ge=0, strict=True)]

_SINGLE_GPU_CONFIG_KEYS = ("gpu_class", "gpu_type", "gpu_type_id", "gpu_name")
_LIST_GPU_CONFIG_KEYS = (
    "gpu_type_priority",
    "gpu_types",
    "gpu_classes",
    "gpuTypeIds",
)
_REQUIRED_COST_KEYS_BY_MODE = {
    CostMode.PER_SECOND: ("per_second_active",),
    CostMode.PER_REQUEST: ("per_request",),
    CostMode.PER_TOKEN: (
        "per_million_input_tokens",
        "per_million_output_tokens",
    ),
}

OPENAI_COMPATIBLE_PROVIDER_TYPES = frozenset(
    {
        ProviderType.SERVERLESS_QUEUE,
        ProviderType.SERVERLESS_LB,
        ProviderType.PUBLIC_ENDPOINT,
    }
)


def expected_openai_base_url(
    provider_type: ProviderType,
    endpoint_id: str,
) -> str:
    """Return the RunPod OpenAI-compatible base URL for a provider surface."""

    if provider_type == ProviderType.POD_LEASE:
        raise ValueError("pod_lease providers do not expose openai_base_url")
    if provider_type == ProviderType.SERVERLESS_LB:
        return f"https://{endpoint_id}.api.runpod.ai/openai/v1"
    return f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1"


def expected_lb_base_url(endpoint_id: str) -> str:
    """Return the RunPod LB base URL for a serverless_lb endpoint."""

    return f"https://{endpoint_id}.api.runpod.ai"


def validate_openai_provider_type(provider_type: ProviderType) -> None:
    """Validate that a provider type is OpenAI-compatible.

    Raises ValueError if the provider type is POD_LEASE, which does not
    support the OpenAI pass-through route.
    """
    if provider_type not in OPENAI_COMPATIBLE_PROVIDER_TYPES:
        allowed = ", ".join(
            t.value for t in sorted(OPENAI_COMPATIBLE_PROVIDER_TYPES, key=lambda x: x.value)
        )
        raise ValueError(
            f"provider_type must be one of: {allowed}; "
            f"got {provider_type.value!r} which is not OpenAI-compatible"
        )


def _validate_cloud_type_volume(
    cloud_type: str | None,
    config: Mapping[str, Any],
) -> None:
    """L2: Reject cloud_type=ALL when networkVolumeId is set.

    RunPod requires cloud_type=SECURE for volume-attached providers.
    Using ALL wastes 50%% of fallback attempts because ALL only attempts
    COMMUNITY first and never retries with SECURE on failure.
    """
    if cloud_type is None:
        cloud_type = config.get("cloud_type") or config.get("cloudType")
    if cloud_type is None:
        return
    cloud_type_upper = str(cloud_type).upper()
    if cloud_type_upper != "ALL":
        return
    has_volume = bool(
        config.get("networkVolumeId")
        or config.get("network_volume_id")
        or config.get("volumeId")
        or config.get("volume_id")
    )
    if has_volume:
        raise ValueError(
            "cloud_type=ALL is not permitted with networkVolumeId; "
            "RunPod requires cloud_type=SECURE for volume-attached providers"
        )


def validate_provider_registration_config(
    *,
    provider_type: ProviderType | None,
    endpoint_id: str | None,
    cloud_type: str | None,
    config: Mapping[str, Any],
) -> None:
    """Validate provider config invariants that are shared across API surfaces."""

    if provider_type is not None:
        _validate_registered_endpoint(provider_type, endpoint_id)
    _validate_cloud_type_volume(cloud_type, config)
    _validate_gpu_config(config)
    _validate_cost_config(config)
    _validate_url_config(provider_type, endpoint_id, config)


def _validate_registered_endpoint(
    provider_type: ProviderType,
    endpoint_id: str | None,
) -> None:
    if provider_type == ProviderType.SERVERLESS_LB and not endpoint_id:
        raise ValueError(
            "serverless_lb providers must register an existing runpod_endpoint_id; "
            "Pitwall does not create LB endpoints"
        )


def _validate_gpu_config(config: Mapping[str, Any]) -> None:
    for key in _SINGLE_GPU_CONFIG_KEYS:
        if key not in config or config[key] is None:
            continue
        value = config[key]
        if not isinstance(value, str):
            raise ValueError(f"config.{key} must be a string")
        validate_canonical_gpu_name(value)

    for key in _LIST_GPU_CONFIG_KEYS:
        if key not in config or config[key] is None:
            continue
        value = config[key]
        if not isinstance(value, list):
            raise ValueError(f"config.{key} must be a list of canonical GPU names")
        if not value:
            raise ValueError(f"config.{key} must include at least one GPU name")
        if not all(isinstance(item, str) for item in value):
            raise ValueError(f"config.{key} must contain only strings")
        validate_canonical_gpu_names(value)


def _validate_cost_config(config: Mapping[str, Any]) -> None:
    raw_cost = config.get("cost")
    if raw_cost is None:
        return
    if not isinstance(raw_cost, Mapping):
        raise ValueError("config.cost must be an object")

    raw_mode = raw_cost.get("mode")
    mode = _parse_cost_mode(raw_mode) if raw_mode is not None else None
    if mode is not None:
        for key in _REQUIRED_COST_KEYS_BY_MODE[mode]:
            _require_non_negative_decimal(raw_cost, key)
        return

    for key in (
        "per_second_active",
        "per_request",
        "per_million_input_tokens",
        "per_million_output_tokens",
    ):
        if key in raw_cost:
            _require_non_negative_decimal(raw_cost, key)


def _parse_cost_mode(value: object) -> CostMode:
    if isinstance(value, CostMode):
        return value
    if isinstance(value, str):
        try:
            return CostMode(value)
        except ValueError as exc:
            allowed = ", ".join(cost_mode.value for cost_mode in CostMode)
            raise ValueError(f"config.cost.mode must be one of: {allowed}") from exc
    raise ValueError("config.cost.mode must be a string")


def _require_non_negative_decimal(cost: Mapping[str, Any], key: str) -> None:
    if key not in cost:
        raise ValueError(f"config.cost.mode requires config.cost.{key}")
    value = cost[key]
    if isinstance(value, bool):
        raise ValueError(f"config.cost.{key} must be a non-negative decimal")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"config.cost.{key} must be a non-negative decimal") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError(f"config.cost.{key} must be a non-negative decimal")


def _validate_url_config(
    provider_type: ProviderType | None,
    endpoint_id: str | None,
    config: Mapping[str, Any],
) -> None:
    openai_base_url = _optional_non_empty_config_string(config, "openai_base_url")
    if openai_base_url is not None:
        if provider_type is None:
            return
        if endpoint_id is None:
            raise ValueError("config.openai_base_url requires runpod_endpoint_id")
        expected = expected_openai_base_url(provider_type, endpoint_id)
        if openai_base_url != expected:
            raise ValueError(
                f"config.openai_base_url must be {expected!r} for "
                f"provider_type {provider_type.value!r}"
            )

    lb_base_url = _optional_non_empty_config_string(config, "lb_base_url")
    if lb_base_url is None:
        return
    if provider_type is not None and provider_type != ProviderType.SERVERLESS_LB:
        raise ValueError("config.lb_base_url is only valid for serverless_lb providers")
    if endpoint_id is None:
        raise ValueError("config.lb_base_url requires runpod_endpoint_id")
    expected = expected_lb_base_url(endpoint_id)
    if lb_base_url.rstrip("/") != expected:
        raise ValueError(
            f"config.lb_base_url must be {expected!r} for runpod_endpoint_id {endpoint_id!r}"
        )


def _optional_non_empty_config_string(
    config: Mapping[str, Any],
    key: str,
) -> str | None:
    if key not in config or config[key] is None:
        return None
    value = config[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"config.{key} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"config.{key} must not include surrounding whitespace")
    return value


class ProviderCreate(PitwallModel):
    """Request body for POST /v1/admin/providers."""

    capability_id: Annotated[str, Field(min_length=1)]
    name: Annotated[str, Field(min_length=1)]
    provider_type: ProviderType
    runpod_endpoint_id: str | None = None
    runpod_template_id: str | None = None
    region: str | None = None
    cloud_type: str | None = None
    config: JsonObject = Field(default_factory=dict)
    priority: Priority = 0
    enabled: bool = True
    health_status: str = "unknown"
    consecutive_failures: NonNegativeInt = 0
    cooldown_trips: NonNegativeInt = 0
    cold_start_p50_ms: NonNegativeInt | None = None
    cold_start_p95_ms: NonNegativeInt | None = None
    recent_error_rate: float = Field(default=0, ge=0, le=1)
    source: CapabilitySource = CapabilitySource.API

    @model_validator(mode="after")
    def _validate_registration_config(self) -> ProviderCreate:
        validate_provider_registration_config(
            provider_type=self.provider_type,
            endpoint_id=self.runpod_endpoint_id,
            cloud_type=self.cloud_type,
            config=self.config,
        )
        return self


class ProviderPatch(PitwallModel):
    """Request body for PATCH /v1/admin/providers/{id}.

    All fields are optional — only supplied fields are merged.
    """

    name: Annotated[str, Field(min_length=1)] | None = None
    provider_type: ProviderType | None = None
    runpod_endpoint_id: str | None = None
    runpod_template_id: str | None = None
    region: str | None = None
    cloud_type: str | None = None
    config: JsonObject | None = None
    priority: Priority | None = None
    enabled: bool | None = None
    health_status: str | None = None
    consecutive_failures: NonNegativeInt | None = None
    cooldown_trips: NonNegativeInt | None = None
    cold_start_p50_ms: NonNegativeInt | None = None
    cold_start_p95_ms: NonNegativeInt | None = None
    recent_error_rate: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _validate_registration_config(self) -> ProviderPatch:
        if self.config is not None:
            validate_provider_registration_config(
                provider_type=self.provider_type,
                endpoint_id=self.runpod_endpoint_id,
                cloud_type=self.cloud_type,
                config=self.config,
            )
        return self


class ProviderListFilter(PitwallModel):
    """Query parameters for GET /v1/providers."""

    capability_id: str | None = None
    enabled: bool | None = None
    provider_type: ProviderType | None = None


class ProviderResponse(PitwallModel):
    """Response body for GET /v1/providers."""

    id: str
    capability_id: str
    name: str
    provider_type: ProviderType
    runpod_endpoint_id: str | None = None
    runpod_template_id: str | None = None
    region: str | None = None
    cloud_type: str | None = None
    config: JsonObject = Field(default_factory=dict)
    priority: Priority
    enabled: bool = True
    health_status: str = "unknown"
    consecutive_failures: NonNegativeInt = 0
    cooldown_trips: NonNegativeInt = 0
    cold_start_p50_ms: NonNegativeInt | None = None
    cold_start_p95_ms: NonNegativeInt | None = None
    recent_error_rate: float = Field(default=0, ge=0, le=1)
    cooldown_until: str | None = None
    source: CapabilitySource = CapabilitySource.API
    last_applied_yaml_hash: str | None = None
    updated_at: str


class ProviderHealthResponse(PitwallModel):
    """Response body for GET /v1/providers/{id}/health."""

    id: str
    name: str
    health_status: str
    consecutive_failures: NonNegativeInt = 0
    cooldown_trips: NonNegativeInt = 0
    cooldown_until: str | None = None
    recent_error_rate: float
    updated_at: str


class ProviderHibernateResponse(PitwallModel):
    """Response body for POST /v1/admin/providers/{id}/hibernate."""

    id: str
    name: str
    health_status: str
    cooldown_until: str | None = None
    enabled: bool


class EndpointCostConfig(PitwallModel):
    """Cost configuration for endpoint registration.

    Fields:
        per_second_active: Cost per second when container is active (container-seconds).
            Used for serverless_queue and serverless_lb provider types.
        per_request: Flat cost per request. Used for public_endpoint provider types.
        per_million_input_tokens: Cost per million input tokens. Used for per_token cost mode.
        per_million_output_tokens: Cost per million output tokens. Used for per_token cost mode.
    """

    mode: CostMode | None = Field(default=None, description="Cost estimator mode")
    per_second_active: float | None = Field(
        default=None,
        ge=0,
        description="Cost per active container-second (USD)",
    )
    per_request: float | None = Field(
        default=None,
        ge=0,
        description="Flat cost per request (USD)",
    )
    per_million_input_tokens: float | None = Field(
        default=None,
        ge=0,
        description="Cost per million input tokens (USD)",
    )
    per_million_output_tokens: float | None = Field(
        default=None,
        ge=0,
        description="Cost per million output tokens (USD)",
    )

    @model_validator(mode="after")
    def _validate_cost_mode_requirements(self) -> EndpointCostConfig:
        if self.mode is None:
            return self
        cost_values = self.model_dump(mode="python", exclude_none=True)
        for key in _REQUIRED_COST_KEYS_BY_MODE[self.mode]:
            _require_non_negative_decimal(cost_values, key)
        return self


class EndpointWorkersConfig(PitwallModel):
    """Worker scaling configuration for serverless endpoints.

    Fields:
        workers_min: Minimum number of always-on workers. Set to 0 to hibernate.
            Default is 0 for hibernated endpoints, 1 for minimum always-on.
        workers_max: Maximum number of workers the endpoint can scale to.
            Controls the rate limit ceiling: max(base_limit, workers_max x per_worker_limit).
    """

    workers_min: NonNegativeInt = Field(
        default=0,
        description="Minimum always-on worker count (0 = hibernated)",
    )
    workers_max: NonNegativeInt | None = Field(
        default=None,
        ge=0,
        description="Maximum worker count for auto-scaling",
    )


class EndpointRegistrationConfig(PitwallModel):
    """Configuration fields for endpoint registration.

    This schema explicitly defines the fields used when registering
    a RunPod endpoint with Pitwall.

    Fields:
        gpu_class: Canonical RunPod GPU type ID (e.g., "NVIDIA H100 NVL", "RTX 4090").
            Must match RunPod's gpuTypeId exactly. Used for routing and scoring.
        cost: Cost parameters for the endpoint. Mode is determined by capability cost_mode.
        workers: Worker scaling settings for serverless endpoints.
        idle_timeout_minutes: Idle timeout before container scales to zero.
            Applies to serverless_queue and serverless_lb provider types.
        flash_boot_verified: Whether FlashBoot has been verified in the RunPod console.
            FlashBoot reduces cold-start by ~35s but has a runpodctl 0.x regression
            that silently no-ops on create and fails on update. Must be verified manually.
        max_payload_mb: Maximum payload size in megabytes. Defaults to 30 for LB endpoints.
        request_timeout_s: Request timeout in seconds. Default is 330 for LB endpoints.
        custom_paths: Custom HTTP paths exposed by the endpoint workers.
            Maps path name to route (e.g., {"embed": "/embed", "health": "/ping"}).
        lb_base_url: Base URL for load-balancing serverless endpoints.
            Auto-constructed from endpoint_id if not provided.
        openai_base_url: OpenAI-compatible base URL.
            Must be composed from endpoint_id and provider_type if provided.
    """

    gpu_class: Annotated[
        str,
        Field(min_length=1, description="Canonical RunPod GPU type ID"),
    ]
    cost: EndpointCostConfig = Field(default_factory=EndpointCostConfig)
    workers: EndpointWorkersConfig = Field(default_factory=EndpointWorkersConfig)
    idle_timeout_minutes: Annotated[
        int,
        Field(
            ge=0,
            le=60,
            description="Idle timeout in minutes before scale-to-zero (0-60)",
        ),
    ] = 0
    flash_boot_verified: bool = Field(
        default=False,
        description="Whether FlashBoot setting has been verified in RunPod console",
    )
    max_payload_mb: Annotated[
        int,
        Field(ge=1, le=1024, description="Maximum request payload size in MB"),
    ] = 30
    request_timeout_s: Annotated[
        int,
        Field(ge=1, le=900, description="Request timeout in seconds"),
    ] = 330
    custom_paths: JsonObject = Field(
        default_factory=dict,
        description="Custom path mappings (e.g., {'embed': '/embed'})",
    )
    lb_base_url: str | None = Field(
        default=None,
        description="LB endpoint base URL (auto-constructed if not provided)",
    )
    openai_base_url: str | None = Field(
        default=None,
        description="OpenAI-compatible endpoint base URL",
    )

    @field_validator("gpu_class")
    @classmethod
    def _validate_gpu_class(cls, gpu_class: str) -> str:
        return validate_canonical_gpu_name(gpu_class)


class EndpointRegistrationRequest(PitwallModel):
    """Request body for endpoint registration CLI and API.

    This schema consolidates the fields required to register a RunPod
    endpoint with Pitwall: endpoint ID, provider type, cost parameters,
    GPU class, worker settings, idle timeout, and FlashBoot verification.

    Fields:
        endpoint_id: RunPod endpoint ID (e.g., "eptest00000000").
            For serverless_lb, this is the LB endpoint ID used in URLs like
            https://{endpoint_id}.api.runpod.ai.
        provider_type: RunPod surface type (serverless_queue, serverless_lb,
            public_endpoint, pod_lease).
        capability_id: ID of the capability this endpoint fulfills.
        name: Human-readable name for this provider.
        region: RunPod region ID (e.g., "US-KS-2", "US-CA-2").
        config: Endpoint configuration including gpu_class, cost, workers, etc.
        priority: Routing priority (lower = preferred). Default is 0.
    """

    endpoint_id: Annotated[
        str,
        Field(min_length=1, description="RunPod endpoint ID"),
    ]
    provider_type: ProviderType
    capability_id: Annotated[
        str,
        Field(min_length=1, description="Capability ID this endpoint fulfills"),
    ]
    name: Annotated[
        str,
        Field(min_length=1, description="Human-readable provider name"),
    ]
    region: str | None = Field(
        default=None,
        description="RunPod region ID (e.g., US-KS-2)",
    )
    config: EndpointRegistrationConfig
    priority: Priority = Field(
        default=0,
        description="Routing priority (lower = preferred)",
    )

    @model_validator(mode="after")
    def _validate_registration_config(self) -> EndpointRegistrationRequest:
        config = self.config.model_dump(mode="python", exclude_none=True)
        validate_provider_registration_config(
            provider_type=self.provider_type,
            endpoint_id=self.endpoint_id,
            cloud_type=None,
            config=config,
        )
        if self.config.lb_base_url is not None:
            if self.provider_type != ProviderType.SERVERLESS_LB:
                raise ValueError("config.lb_base_url is only valid for serverless_lb providers")
            expected = expected_lb_base_url(self.endpoint_id)
            if self.config.lb_base_url.rstrip("/") != expected:
                raise ValueError(f"config.lb_base_url must be {expected!r}")
        return self


__all__ = [
    "EndpointCostConfig",
    "EndpointRegistrationConfig",
    "EndpointRegistrationRequest",
    "OPENAI_COMPATIBLE_PROVIDER_TYPES",
    "expected_lb_base_url",
    "expected_openai_base_url",
    "ProviderCreate",
    "ProviderPatch",
    "ProviderListFilter",
    "ProviderResponse",
    "ProviderHealthResponse",
    "ProviderHibernateResponse",
    "validate_openai_provider_type",
    "validate_provider_registration_config",
]
