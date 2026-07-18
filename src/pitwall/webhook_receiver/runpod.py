"""RunPod webhook event models and normalization.

Accepts raw RunPod webhook payloads that arrive with any of the observed
id field variants and normalizes them into a single Pydantic model.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_ID_FIELD_VARIANTS = ("id", "job_id", "jobId", "runpod_job_id")
_ATTEMPT_HEADER_VARIANTS = (
    "X-RunPod-Attempt",
    "X-Runpod-Attempt",
    "RunPod-Attempt",
    "Runpod-Attempt",
)


class RunPodWebhookEvent(BaseModel):
    """Normalized RunPod webhook event.

    RunPod delivers webhooks with job status updates. The job ID can appear
    under several different field names depending on which RunPod API
    endpoint generated the webhook. This model normalizes all observed
    variants into a single canonical form.

    Attributes:
        runpod_job_id: Normalized RunPod job ID (always non-empty).
        status: RunPod status string (IN_QUEUE, IN_PROGRESS, COMPLETED, FAILED, CANCELLED).
        attempt: Delivery attempt number (1-3, default 1).
        output: Job output dict when present (optional).
        error: Error message string when present (optional).
        raw: Original unparsed webhook payload for debugging.
    """

    runpod_job_id: str = Field(description="Normalized RunPod job ID.")
    status: str = Field(description="RunPod status string.")
    attempt: int = Field(default=1, ge=1, le=3, description="Webhook delivery attempt (1-3).")
    output: dict[str, Any] | None = Field(default=None, description="Job output when present.")
    error: str | None = Field(default=None, description="Error message when present.")
    raw: dict[str, Any] = Field(default_factory=dict, description="Original webhook payload.")

    model_config = {"extra": "allow"}


def _extract_id(payload: dict[str, Any]) -> str | None:
    for field_name in _ID_FIELD_VARIANTS:
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_attempt(payload: dict[str, Any], headers: dict[str, str]) -> int:
    payload_attempt = payload.get("attempt")
    if isinstance(payload_attempt, int) and 1 <= payload_attempt <= 3:
        return payload_attempt

    for header_name in _ATTEMPT_HEADER_VARIANTS:
        header_value = headers.get(header_name) or headers.get(header_name.lower())
        if header_value and header_value.strip():
            try:
                attempt = int(header_value.strip())
                if 1 <= attempt <= 3:
                    return attempt
            except ValueError:
                pass

    return 1


def normalize_runpod_webhook(
    payload: dict[str, Any],
    headers: dict[str, str],
) -> RunPodWebhookEvent:
    """Normalize a raw RunPod webhook payload into a RunPodWebhookEvent.

    Args:
        payload: Raw webhook JSON payload dictionary.
        headers: HTTP headers from the webhook request.

    Returns:
        Normalized RunPodWebhookEvent instance.

    Raises:
        ValueError: If no valid job ID is found in the payload.
    """
    job_id = _extract_id(payload)
    if not job_id:
        raise ValueError("No valid RunPod job ID found in webhook payload")

    return RunPodWebhookEvent(
        runpod_job_id=job_id,
        status=payload.get("status", ""),
        attempt=_extract_attempt(payload, headers),
        output=payload.get("output"),
        error=payload.get("error"),
        raw=payload,
    )
