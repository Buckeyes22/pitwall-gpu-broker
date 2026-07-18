"""Pydantic v2 schemas for the Inference API surface."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AliasChoices, ConfigDict, Field

from pitwall.core.models import PitwallModel


class InferenceRequest(PitwallModel):
    """Request body for POST /v1/inference.

    The ``capability_id`` field accepts a capability name or registry id.
    All other fields are passed through to the selected RunPod provider
    verbatim after capability-specific validation.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
        use_enum_values=False,
    )

    capability_id: Annotated[
        str,
        Field(
            min_length=1,
            pattern=r"^[^\x00]+$",
            description="Capability name or ID to invoke",
            validation_alias=AliasChoices("capability_id", "capability", "capability_name"),
        ),
    ]
    provider_id: Annotated[str, Field(min_length=1, pattern=r"^[^\x00]+$")] | None = Field(
        default=None, description="Specific provider ID to use (optional)"
    )
    dry_run: bool = Field(
        default=False, description="Run routing and cost estimation without calling RunPod"
    )
    idempotency_key: (
        Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[^\x00]+$")] | None
    ) = Field(default=None, description="Idempotency key for the request")


class InferenceResponse(PitwallModel):
    """Response body for POST /v1/inference.

    Returns the RunPod result verbatim for the selected provider surface.
    """

    workload_id: str
    result: dict[str, Any]


__all__ = [
    "InferenceRequest",
    "InferenceResponse",
]
