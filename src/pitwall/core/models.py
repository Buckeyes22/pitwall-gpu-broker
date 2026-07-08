"""Pydantic v2 domain models for Pitwall registry and runtime records."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from pitwall.core.enums import (
    CapabilityClass,
    CapabilityHint,
    CapabilitySource,
    CostMode,
    LeaseRenewalPolicy,
    LeaseState,
    ProviderType,
    ResultDelivery,
    WorkloadState,
)

JsonObject = dict[str, Any]
UsdAmount = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=6)]


def _require_string_enum_value(value: object) -> object:
    if isinstance(value, str):
        return value
    raise ValueError("enum values must be strings")


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone information")
    return value.astimezone(dt.UTC)


UTCDateTime = Annotated[dt.datetime, AfterValidator(_as_utc)]
StrictCapabilityClass = Annotated[CapabilityClass, BeforeValidator(_require_string_enum_value)]
StrictCapabilityHint = Annotated[CapabilityHint, BeforeValidator(_require_string_enum_value)]
StrictCapabilitySource = Annotated[CapabilitySource, BeforeValidator(_require_string_enum_value)]
StrictCostMode = Annotated[CostMode, BeforeValidator(_require_string_enum_value)]
StrictLeaseRenewalPolicy = Annotated[
    LeaseRenewalPolicy, BeforeValidator(_require_string_enum_value)
]
StrictLeaseState = Annotated[LeaseState, BeforeValidator(_require_string_enum_value)]
StrictResultDelivery = Annotated[ResultDelivery, BeforeValidator(_require_string_enum_value)]
StrictWorkloadState = Annotated[WorkloadState, BeforeValidator(_require_string_enum_value)]


NonEmptyString = Annotated[str, Field(min_length=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class PitwallModel(BaseModel):
    """Base model policy shared by public Pitwall domain objects."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        use_enum_values=False,
    )


class CapabilityDefaults(PitwallModel):
    """Default execution settings attached to a capability."""

    execution_timeout_ms: NonNegativeInt = 60_000
    ttl_ms: NonNegativeInt = 300_000
    result_delivery: StrictResultDelivery = ResultDelivery.SYNC


class Capability(PitwallModel):
    """What a consumer asks Pitwall to fulfill."""

    id: NonEmptyString
    name: NonEmptyString
    version: NonEmptyString
    class_: StrictCapabilityClass = Field(
        validation_alias=AliasChoices("class", "class_", "capability_class"),
        serialization_alias="class",
    )
    description: str | None = None
    input_schema: JsonObject = Field(default_factory=dict)
    output_schema: JsonObject = Field(default_factory=dict)
    defaults: CapabilityDefaults = Field(default_factory=CapabilityDefaults)
    cost_mode: StrictCostMode
    hints_supported: list[StrictCapabilityHint] = Field(default_factory=list)
    source: StrictCapabilitySource = CapabilitySource.API
    last_applied_yaml_hash: str | None = None
    enabled: bool = True
    created_at: UTCDateTime
    updated_at: UTCDateTime

    @property
    def capability_class(self) -> CapabilityClass:
        """Return the capability class using a non-reserved Python name."""
        return self.class_


class Workload(PitwallModel):
    """A single persisted unit of consumer-requested work."""

    id: NonEmptyString
    capability_id: NonEmptyString
    provider_id: NonEmptyString
    type: NonEmptyString
    state: StrictWorkloadState
    runpod_job_id: str | None = None
    idempotency_key: str | None = None
    input: JsonObject | None = None
    result: JsonObject | None = None
    submitted_at: UTCDateTime
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None
    execution_ms: NonNegativeInt | None = None
    queue_ms: NonNegativeInt | None = None
    cold_start_ms: NonNegativeInt | None = None
    input_bytes: NonNegativeInt | None = None
    output_bytes: NonNegativeInt | None = None
    cost_estimate_usd: UsdAmount | None = None
    cost_actual_usd: UsdAmount | None = None
    fallback_chain: list[NonEmptyString] = Field(default_factory=list)
    error: JsonObject | None = None
    langfuse_trace_id: str | None = None


class LeaseTcpEndpoint(PitwallModel):
    """TCP proxy endpoint exposed by RunPod for a lease."""

    host: NonEmptyString
    port: Annotated[int, Field(ge=1, le=65_535)]


class LeaseEndpoints(PitwallModel):
    """HTTP and TCP endpoints observed for a pod lease."""

    http: dict[str, NonEmptyString] = Field(default_factory=dict)
    tcp: dict[str, LeaseTcpEndpoint] = Field(default_factory=dict)


class LeaseReadiness(PitwallModel):
    """Readiness signals required before a lease can become active."""

    runtime_seen_at: UTCDateTime | None = None
    port_mappings_seen_at: UTCDateTime | None = None
    probe_passed_at: UTCDateTime | None = None
    probe_method: str | None = None

    @property
    def has_active_signals(self) -> bool:
        return (
            self.runtime_seen_at is not None
            and self.port_mappings_seen_at is not None
            and self.probe_passed_at is not None
        )


class Lease(PitwallModel):
    """Stateful RunPod pod allocation from create through teardown."""

    id: NonEmptyString
    provider_id: NonEmptyString
    runpod_pod_id: NonEmptyString
    state: StrictLeaseState
    created_at: UTCDateTime
    expires_at: UTCDateTime
    renewal_policy: StrictLeaseRenewalPolicy
    auto_teardown_on_expiry: bool = True
    endpoints: LeaseEndpoints | None = None
    readiness: LeaseReadiness | None = None
    cost_accrued_usd: UsdAmount | None = None
    last_health_at: UTCDateTime | None = None
    terminated_at: UTCDateTime | None = None
    terminated_reason: str | None = None

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> Lease:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.state == LeaseState.ACTIVE:
            if self.endpoints is None:
                raise ValueError("active leases require endpoints")
            if self.readiness is None or not self.readiness.has_active_signals:
                raise ValueError(
                    "active leases require runtime, port mapping, and probe readiness signals"
                )
        return self


StrictProviderType = Annotated[ProviderType, BeforeValidator(_require_string_enum_value)]


class Provider(PitwallModel):
    """A concrete fulfillment binding to a RunPod resource."""

    id: NonEmptyString
    capability_id: NonEmptyString
    name: NonEmptyString
    provider_type: StrictProviderType
    runpod_endpoint_id: str | None = None
    runpod_template_id: str | None = None
    region: str | None = None
    cloud_type: str | None = None
    config: JsonObject = Field(default_factory=dict)
    priority: int = Field(ge=0)
    enabled: bool = True
    health_status: str = "unknown"
    consecutive_failures: NonNegativeInt = 0
    cooldown_trips: NonNegativeInt = 0
    cold_start_p50_ms: NonNegativeInt | None = None
    cold_start_p95_ms: NonNegativeInt | None = None
    recent_error_rate: float = Field(default=0, ge=0, le=1)
    cooldown_until: UTCDateTime | None = None
    source: StrictCapabilitySource = CapabilitySource.API
    last_applied_yaml_hash: str | None = None
    updated_at: UTCDateTime


class ConfigAuditEntry(PitwallModel):
    """A single row in the config mutation audit trail."""

    id: int
    actor: NonEmptyString
    action: NonEmptyString
    entity_type: NonEmptyString
    entity_id: NonEmptyString
    old_value: JsonObject | None = None
    new_value: JsonObject | None = None
    change_reason: str | None = None
    created_at: UTCDateTime


class RateBucket(PitwallModel):
    """Persisted token-bucket state for a RunPod endpoint operation."""

    endpoint_id: NonEmptyString
    operation: NonEmptyString
    capacity: Annotated[int, Field(gt=0)]
    tokens: Annotated[float, Field(ge=0)]
    last_refilled_at: UTCDateTime
    recent_429_at: UTCDateTime | None = None


class WebhookSubscription(PitwallModel):
    """A consumer-registered webhook URL for async job result callbacks."""

    id: NonEmptyString
    consumer: NonEmptyString
    webhook_url: NonEmptyString
    hmac_secret: str | None = Field(
        default=None,
        repr=False,
        description="Secret for HMAC-signed webhook delivery. Never exposed via API.",
    )
    active: bool = True
    created_at: UTCDateTime
    updated_at: UTCDateTime


class WebhookSubscriptionCreate(PitwallModel):
    """Input schema for creating a webhook subscription."""

    consumer: Annotated[str, Field(min_length=1, pattern=r"^[^\x00]+$")]
    webhook_url: Annotated[str, Field(min_length=1, pattern=r"^[^\x00]+$")]


class WebhookSubscriptionResponse(PitwallModel):
    """Output schema for webhook subscription read operations.

    The hmac_secret is explicitly excluded to prevent accidental exposure.
    """

    id: NonEmptyString
    consumer: NonEmptyString
    webhook_url: NonEmptyString
    active: bool
    created_at: UTCDateTime
    updated_at: UTCDateTime


class WebhookSubscriptionCreated(WebhookSubscriptionResponse):
    """Creation response containing the signing secret exactly once."""

    signing_secret: NonEmptyString = Field(repr=False)


class WebhookSecretRotationResponse(PitwallModel):
    """Secret rotation response; the new secret is never listable later."""

    id: NonEmptyString
    signing_secret: NonEmptyString = Field(repr=False)


class WebhookDeliveryFailure(PitwallModel):
    """Records a failed consumer webhook delivery attempt.

    Delivery failures are tracked separately from workload state so that
    transient delivery failures do not pollute workload state. Bounded
    retries (max 4 attempts) are applied before giving up.
    """

    id: int | None = None
    workload_id: NonEmptyString
    subscription_id: Annotated[int, Field(ge=1)]
    attempt: Annotated[int, Field(ge=1, le=4)]
    attempted_at: UTCDateTime
    next_retry_at: UTCDateTime | None = None
    payload: JsonObject
    status_code: Annotated[int | None, Field(ge=100, le=599)] = None
    error_message: str | None = None


__all__ = [
    "Capability",
    "CapabilityDefaults",
    "ConfigAuditEntry",
    "JsonObject",
    "Lease",
    "LeaseEndpoints",
    "LeaseReadiness",
    "LeaseTcpEndpoint",
    "PitwallModel",
    "Provider",
    "RateBucket",
    "UTCDateTime",
    "UsdAmount",
    "WebhookDeliveryFailure",
    "WebhookSubscription",
    "WebhookSubscriptionCreate",
    "WebhookSubscriptionCreated",
    "WebhookSubscriptionResponse",
    "WebhookSecretRotationResponse",
    "Workload",
]
