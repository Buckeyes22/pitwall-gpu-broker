"""Pydantic v2 schemas for the Lease API surface."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from pydantic import ConfigDict, Field, model_validator

from pitwall.core.enums import LeaseRenewalPolicy, LeaseState
from pitwall.core.models import (
    LeaseEndpoints,
    LeaseReadiness,
    PitwallModel,
    UsdAmount,
)

_IMAGE_CHANGE_FIELDS = (
    "image_ref",
    "worker_image",
    "image",
    "runpod_template_id",
    "template_id",
    "template_name",
)
_GPU_CHANGE_FIELDS = (
    "gpu_class",
    "gpu_type",
    "gpu_type_id",
    "gpu_name",
    "gpu_types",
    "gpu_type_ids",
    "gpuTypeIds",
    "gpu_classes",
    "gpu_type_priority",
    "gpu_count",
    "gpu_type_priority_mode",
    "gpu_selection_priority",
)
_VOLUME_CHANGE_FIELDS = (
    "volume_id",
    "network_volume_id",
    "volume_mount",
    "volume_mount_path",
)
_CHANGE_SET_AXES = (
    _IMAGE_CHANGE_FIELDS,
    _GPU_CHANGE_FIELDS,
    _VOLUME_CHANGE_FIELDS,
)
_SUPPORTED_PATCH_FIELDS = frozenset(
    {"renewal_policy", "auto_teardown_on_expiry", "idempotency_key"}
)


class LeaseCreate(PitwallModel):
    """Request body for POST /v1/leases."""

    capability_id: Annotated[
        str,
        Field(min_length=1, pattern=r"^[^\x00]+$", description="Capability ID to fulfill"),
    ]
    provider_id: (
        Annotated[
            str,
            Field(min_length=1, pattern=r"^[^\x00]+$", description="Provider ID to use"),
        ]
        | None
    ) = None
    dry_run: bool = Field(
        default=False, description="Run routing and cost estimation without creating a pod"
    )
    idempotency_key: (
        Annotated[
            str,
            Field(min_length=1, max_length=255, pattern=r"^[^\x00]+$"),
        ]
        | None
    ) = Field(default=None, description="Idempotency key for the request")


class LeaseResponse(PitwallModel):
    """Response body for GET /v1/leases/{id} and POST /v1/leases."""

    id: str
    provider_id: str
    runpod_pod_id: str
    state: LeaseState
    created_at: str
    expires_at: str
    renewal_policy: LeaseRenewalPolicy
    auto_teardown_on_expiry: bool = True
    endpoints: LeaseEndpoints | None = None
    readiness: LeaseReadiness | None = None
    cost_accrued_usd: UsdAmount | None = None
    last_health_at: str | None = None
    terminated_at: str | None = None
    terminated_reason: str | None = None


class LeasePatch(PitwallModel):
    """Request body for PATCH /v1/leases/{id}.

    All fields are optional — only supplied fields are merged.
    change_set validation (L16) rejects requests that simultaneously
    change multiple axes (image, GPU, volume).
    """

    model_config = ConfigDict(extra="allow")

    renewal_policy: LeaseRenewalPolicy | None = None
    auto_teardown_on_expiry: bool | None = None
    idempotency_key: (
        Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[^\x00]+$")] | None
    ) = None

    @model_validator(mode="after")
    def reject_explicit_null_mutations(self) -> LeasePatch:
        null_fields = [
            field
            for field in ("renewal_policy", "auto_teardown_on_expiry")
            if field in self.model_fields_set and getattr(self, field) is None
        ]
        if null_fields:
            raise ValueError(f"lease mutation fields may not be null: {', '.join(null_fields)}")
        return self


def lease_patch_conflicting_fields(patch: LeasePatch | Mapping[str, object]) -> list[str]:
    """Return supplied fields when a lease PATCH spans paid-launch axes."""

    supplied_fields = _supplied_patch_fields(patch)
    fields_by_axis = [
        [field for field in axis_fields if field in supplied_fields]
        for axis_fields in _CHANGE_SET_AXES
    ]
    conflicting_axes = [fields for fields in fields_by_axis if fields]
    if len(conflicting_axes) <= 1:
        return []
    return [field for fields in conflicting_axes for field in fields]


def lease_patch_unsupported_fields(patch: LeasePatch | Mapping[str, object]) -> list[str]:
    """Return accepted compatibility fields that are not currently mutable."""

    supplied_fields = _supplied_patch_fields(patch)
    return sorted(supplied_fields - _SUPPORTED_PATCH_FIELDS)


def _supplied_patch_fields(patch: LeasePatch | Mapping[str, object]) -> set[str]:
    if isinstance(patch, LeasePatch):
        return set(patch.model_fields_set)
    return {str(key) for key in patch}


class LeaseRenew(PitwallModel):
    """Request body for POST /v1/leases/{id}/renew."""

    extends_minutes: Annotated[
        int, Field(ge=1, le=43200, description="Minutes to extend the lease")
    ] = 60
    idempotency_key: (
        Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[^\x00]+$")] | None
    ) = None


class LeaseStop(PitwallModel):
    """Request body for POST /v1/leases/{id}/stop."""

    reason: (
        Annotated[
            str,
            Field(min_length=1, max_length=500, pattern=r"^[^\x00]+$"),
        ]
        | None
    ) = None


class LeaseDelete(PitwallModel):
    """Request body for DELETE /v1/leases/{id}."""


__all__ = [
    "LeaseCreate",
    "LeaseDelete",
    "LeasePatch",
    "LeaseRenew",
    "LeaseResponse",
    "LeaseStop",
    "lease_patch_conflicting_fields",
    "lease_patch_unsupported_fields",
]
