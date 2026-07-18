"""Pydantic v2 schemas for the Jobs API surface."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from pitwall.core.models import PitwallModel


class JobSubmitRequest(PitwallModel):
    """Request body for POST /v1/jobs.

    Submits an asynchronous workload for processing by a capability provider.
    """

    capability_id: Annotated[
        str,
        Field(
            min_length=1,
            description="Capability name or ID to invoke",
        ),
    ]
    input: Annotated[
        dict[str, Any],
        Field(
            description="Input payload passed to the capability provider",
        ),
    ]
    provider_id: Annotated[str, Field(min_length=1)] | None = Field(
        default=None, description="Specific provider ID to use (optional)"
    )
    dry_run: bool = Field(
        default=False, description="Run routing and cost estimation without calling RunPod"
    )
    idempotency_key: Annotated[str, Field(min_length=1, max_length=255)] | None = Field(
        default=None, description="Idempotency key for the request"
    )
    webhook_url: Annotated[str, Field(min_length=1, max_length=2048)] | None = Field(
        default=None, description="URL to receive a webhook on job completion"
    )


class JobResponse(PitwallModel):
    """Response body for POST /v1/jobs.

    Returns the workload metadata for the submitted async job.
    """

    workload_id: str
    state: str


__all__ = [
    "JobResponse",
    "JobSubmitRequest",
]
