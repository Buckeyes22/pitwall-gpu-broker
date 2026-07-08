"""Pydantic v2 schemas for the Capability API surface."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from pitwall.core.enums import (
    CapabilityClass,
    CapabilityHint,
    CapabilitySource,
    CostMode,
    ResultDelivery,
)
from pitwall.core.models import JsonObject, NonNegativeInt, PitwallModel


class CapabilityDefaultsCreate(PitwallModel):
    """Defaults sub-object for capability creation."""

    execution_timeout_ms: NonNegativeInt = 60_000
    ttl_ms: NonNegativeInt = 300_000
    result_delivery: ResultDelivery = ResultDelivery.SYNC


class CapabilityCreate(PitwallModel):
    """Request body for POST /v1/admin/capabilities."""

    name: Annotated[str, Field(min_length=1)]
    version: Annotated[str, Field(min_length=1)]
    class_: CapabilityClass = Field(
        validation_alias="class",
        serialization_alias="class",
    )
    description: str | None = None
    input_schema: JsonObject = Field(default_factory=dict)
    output_schema: JsonObject = Field(default_factory=dict)
    defaults: CapabilityDefaultsCreate = Field(default_factory=CapabilityDefaultsCreate)
    cost_mode: CostMode
    hints_supported: list[CapabilityHint] = Field(default_factory=list)
    source: CapabilitySource = CapabilitySource.API


class CapabilityPatch(PitwallModel):
    """Request body for PATCH /v1/admin/capabilities/{id}.

    All fields are optional — only supplied fields are merged.
    """

    name: Annotated[str, Field(min_length=1)] | None = None
    version: Annotated[str, Field(min_length=1)] | None = None
    class_: CapabilityClass | None = Field(
        default=None,
        validation_alias="class",
        serialization_alias="class",
    )
    description: str | None = None
    input_schema: JsonObject | None = None
    output_schema: JsonObject | None = None
    defaults: CapabilityDefaultsCreate | None = None
    cost_mode: CostMode | None = None
    hints_supported: list[CapabilityHint] | None = None


class CapabilityListFilter(PitwallModel):
    """Query parameters for GET /v1/capabilities."""

    class_: CapabilityClass | None = Field(
        default=None,
        validation_alias="class",
        serialization_alias="class",
    )
    cost_mode: CostMode | None = None
    source: CapabilitySource | None = None
    enabled: bool | None = None


class CapabilityResponse(PitwallModel):
    """Response body for GET /v1/capabilities/{name}."""

    id: str
    name: str
    version: str
    class_: CapabilityClass = Field(
        validation_alias="class",
        serialization_alias="class",
    )
    description: str | None = None
    input_schema: JsonObject = Field(default_factory=dict)
    output_schema: JsonObject = Field(default_factory=dict)
    defaults: CapabilityDefaultsCreate = Field(default_factory=CapabilityDefaultsCreate)
    cost_mode: CostMode
    hints_supported: list[CapabilityHint] = Field(default_factory=list)
    source: CapabilitySource = CapabilitySource.API
    last_applied_yaml_hash: str | None = None
    enabled: bool = True
    created_at: str
    updated_at: str


__all__ = [
    "CapabilityCreate",
    "CapabilityDefaultsCreate",
    "CapabilityListFilter",
    "CapabilityPatch",
    "CapabilityResponse",
]
