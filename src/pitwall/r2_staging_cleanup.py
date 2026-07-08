"""R2 staging prefix cleanup for killed workload artifacts.

When a workload pod is terminated (via kill switch or teardown), any R2 staging
objects written by that pod should be deleted. This module provides functions to
list and delete R2 objects under workload-specific prefixes.

The primary prefix used for pod log forwarding is ``debug-logs/<pod_id>``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pitwall.r2_temp_credentials import R2TempCredentialError

log = logging.getLogger("pitwall.r2_staging_cleanup")

DEFAULT_R2_DEBUG_LOG_PREFIX = "debug-logs/"


@dataclass(frozen=True)
class StagedPodArtifacts:
    """R2 staging artifacts for a single killed pod."""

    pod_id: str
    pod_name: str
    objects_deleted: int
    errors: list[str]


class R2StagingCleanupError(R2TempCredentialError):
    """Raised when R2 staging cleanup fails."""


def _r2_client(
    endpoint: str,
    access_key: str,
    secret_key: str,
    *,
    timeout_s: float = 30.0,
) -> Any:
    """Create an S3 client configured for R2/S3-compatible storage."""
    try:
        import boto3
        from botocore.config import Config
    except ModuleNotFoundError as exc:
        raise R2StagingCleanupError(
            "R2 staging cleanup requires the optional storage dependencies"
        ) from exc

    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        region_name="auto",
        connect_timeout=timeout_s,
        read_timeout=timeout_s,
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=config,
    )


def delete_pod_staging_prefix(
    pod_id: str,
    *,
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    prefix: str = DEFAULT_R2_DEBUG_LOG_PREFIX,
) -> int:
    """Delete all R2 objects under the prefix for a specific pod.

    Objects are deleted under ``<prefix><pod_id>/`` and
    ``<prefix><pod_id>.log`` (for backward compatibility with the
    single-file log forwarding pattern).

    Returns the count of objects deleted.

    Raises:
        R2StagingCleanupError: If the R2 API call fails.
    """
    if not endpoint.strip():
        raise R2StagingCleanupError("R2 endpoint is required for staging cleanup")
    if not access_key.strip():
        raise R2StagingCleanupError("R2 access key is required for staging cleanup")
    if not secret_key.strip():
        raise R2StagingCleanupError("R2 secret key is required for staging cleanup")
    if not bucket.strip():
        raise R2StagingCleanupError("R2 bucket is required for staging cleanup")
    if not pod_id.strip():
        raise R2StagingCleanupError("pod_id is required for staging cleanup")

    client = _r2_client(endpoint, access_key, secret_key)

    deleted = 0
    errors: list[str] = []

    prefix_patterns = [
        f"{prefix}{pod_id}/",
        f"{prefix}{pod_id}.log",
    ]

    for pattern in prefix_patterns:
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=pattern):
                objects = page.get("Contents", []) or []
                for obj in objects:
                    key = obj.get("Key")
                    if key:
                        try:
                            client.delete_object(Bucket=bucket, Key=key)
                            deleted += 1
                            log.debug("deleted R2 object %s/%s", bucket, key)
                        except Exception as exc:  # pragma: no cover  # reason: record per-object delete failure and continue cleanup
                            errors.append(f"delete {key}: {exc}")
        except Exception as exc:  # pragma: no cover  # reason: record listing failure per pattern and continue cleanup
            errors.append(f"list/iterate {pattern}: {exc}")

    if errors:
        log.warning("R2 staging cleanup for pod %s had errors: %s", pod_id, errors)

    return deleted


def cleanup_staging_for_pods(
    pods: list[dict[str, Any]],
    *,
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    prefix: str = DEFAULT_R2_DEBUG_LOG_PREFIX,
) -> list[StagedPodArtifacts]:
    """Delete R2 staging artifacts for a list of terminated pods.

    Args:
        pods: List of pod dicts, each must have ``id`` and ``name`` keys.
        endpoint: R2 endpoint URL.
        access_key: R2 access key.
        secret_key: R2 secret key.
        bucket: R2 staging bucket name.
        prefix: Prefix under which pod artifacts are stored.

    Returns:
        List of StagedPodArtifacts, one per input pod.
    """
    results: list[StagedPodArtifacts] = []

    for pod in pods:
        pod_id = pod.get("id") or ""
        pod_name = pod.get("name") or ""

        if not pod_id:
            results.append(
                StagedPodArtifacts(
                    pod_id="<unknown>",
                    pod_name=pod_name,
                    objects_deleted=0,
                    errors=["pod has no id"],
                )
            )
            continue

        pod_errors: list[str] = []
        objects_deleted = 0

        try:
            objects_deleted = delete_pod_staging_prefix(
                pod_id,
                endpoint=endpoint,
                access_key=access_key,
                secret_key=secret_key,
                bucket=bucket,
                prefix=prefix,
            )
        except R2StagingCleanupError as exc:
            pod_errors.append(str(exc))

        results.append(
            StagedPodArtifacts(
                pod_id=pod_id,
                pod_name=pod_name,
                objects_deleted=objects_deleted,
                errors=pod_errors,
            )
        )

    return results


__all__ = [
    "DEFAULT_R2_DEBUG_LOG_PREFIX",
    "R2StagingCleanupError",
    "StagedPodArtifacts",
    "cleanup_staging_for_pods",
    "delete_pod_staging_prefix",
]
