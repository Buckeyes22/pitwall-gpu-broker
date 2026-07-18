"""RunPod Serverless endpoint admin helpers."""

from __future__ import annotations

from typing import Any

from pitwall.runpod_client.pods import RunPodError, _rest_request


def hibernate_endpoint(endpoint_id: str) -> dict[str, Any]:
    """Set a Serverless endpoint's always-on worker count to zero."""
    normalized_endpoint_id = _normalize_endpoint_id(endpoint_id)
    response = _rest_request(
        "PATCH",
        f"endpoints/{normalized_endpoint_id}",
        json_body={"workersMin": 0},
    )
    if not isinstance(response, dict):
        raise RunPodError(
            f"hibernate_endpoint({normalized_endpoint_id}) returned unexpected shape: {response!r}"
        )
    return response


def _normalize_endpoint_id(endpoint_id: str) -> str:
    normalized_endpoint_id = endpoint_id.strip().strip("/")
    if not normalized_endpoint_id:
        raise ValueError("endpoint_id must be non-empty")
    if "/" in normalized_endpoint_id:
        raise ValueError("endpoint_id must not contain path separators")
    return normalized_endpoint_id


__all__ = ["hibernate_endpoint"]
