"""Abstraction for optional staging-object storage."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pitwall.r2_temp_credentials import R2TempCredentialEnvConfig

log = logging.getLogger("pitwall.staging_store")

_STS_POD_ENV_KEYS = (
    "R2_ENDPOINT",
    "R2_BUCKET_STAGING",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


class StagingStore(Protocol):
    """Optional staging backend for pod-scoped artifacts."""

    def vend_pod_credentials(self) -> dict[str, str]:
        """Return AWS-style pod credentials, or empty env when unavailable."""

    def cleanup_pod_artifacts(self, pods: list[dict[str, Any]]) -> list[Any]:
        """Delete staged artifacts for pods and return backend cleanup results."""


@dataclass(frozen=True)
class NoOpStagingStore:
    """Default staging store used when object storage is not configured."""

    def vend_pod_credentials(self) -> dict[str, str]:
        return {}

    def cleanup_pod_artifacts(self, pods: list[dict[str, Any]]) -> list[Any]:
        return []


@dataclass(frozen=True)
class CloudflareR2StagingStore:
    """Cloudflare R2 staging backend using temporary pod credentials."""

    environ: Mapping[str, str] | None = None

    def vend_pod_credentials(self) -> dict[str, str]:
        from pitwall.r2_temp_credentials import vend_r2_temp_credential_pod_env

        if self.environ is None:
            return vend_r2_temp_credential_pod_env()
        return vend_r2_temp_credential_pod_env(environ=self.environ)

    def cleanup_pod_artifacts(self, pods: list[dict[str, Any]]) -> list[Any]:
        cleanup_config = _r2_cleanup_config(self.environ)
        if cleanup_config is None:
            return []

        from pitwall.r2_staging_cleanup import cleanup_staging_for_pods

        return cleanup_staging_for_pods(pods, **cleanup_config)


def get_staging_store(environ: Mapping[str, str] | None = None) -> StagingStore:
    """Return the configured staging store, defaulting to a no-op store."""

    env = os.environ if environ is None else environ
    if (
        _has_complete_sts_pod_env(env)
        or R2TempCredentialEnvConfig.from_env(env) is not None
        or _has_complete_r2_cleanup_env(env)
    ):
        return CloudflareR2StagingStore(environ=environ)
    return NoOpStagingStore()


def _has_complete_sts_pod_env(env: Mapping[str, str]) -> bool:
    return all(_non_empty(env.get(key)) for key in _STS_POD_ENV_KEYS)


def _has_complete_r2_cleanup_env(env: Mapping[str, str]) -> bool:
    return all(
        (
            _non_empty(env.get("R2_ENDPOINT")),
            _non_empty(env.get("R2_ACCESS_KEY")),
            _non_empty(env.get("R2_SECRET_KEY")),
            _non_empty(env.get("R2_BUCKET_STAGING") or env.get("R2_BUCKET")),
        )
    )


def _non_empty(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _r2_cleanup_config(environ: Mapping[str, str] | None) -> dict[str, str] | None:
    env = os.environ if environ is None else environ
    endpoint = _non_empty(env.get("R2_ENDPOINT"))
    access_key = _non_empty(env.get("R2_ACCESS_KEY"))
    secret_key = _non_empty(env.get("R2_SECRET_KEY"))
    bucket = _non_empty(env.get("R2_BUCKET_STAGING") or env.get("R2_BUCKET"))
    prefix = _non_empty(env.get("R2_TEMP_CREDENTIAL_PREFIXES")) or _non_empty(
        env.get("PITWALL_R2_TEMP_CREDENTIAL_PREFIXES")
    )

    if not all((endpoint, access_key, secret_key, bucket)):
        if environ is not None:
            return None
        try:
            from pitwall.config import get_settings

            settings = get_settings()
        except (
            Exception
        ) as exc:  # pragma: no cover  # reason: settings failure only disables optional cleanup
            log.warning("could not load settings for staging cleanup: %s", exc)
            return None

        endpoint = _non_empty(settings.r2_endpoint)
        access_key = _non_empty(settings.r2_access_key)
        secret_key = _non_empty(settings.r2_secret_key)
        bucket = _non_empty(settings.r2_bucket_staging)
        if not prefix:
            prefix = _non_empty(settings.r2_temp_credential_prefixes)

    if not all((endpoint, access_key, secret_key, bucket)):
        log.debug(
            "R2 cleanup skipped: endpoint=%s access_key=%s bucket=%s",
            bool(endpoint),
            bool(access_key),
            bucket or "",
        )
        return None

    resolved_prefix = (prefix or "debug-logs/").rstrip("/") + "/"
    return {
        "endpoint": endpoint or "",
        "access_key": access_key or "",
        "secret_key": secret_key or "",
        "bucket": bucket or "",
        "prefix": resolved_prefix,
    }


__all__ = [
    "CloudflareR2StagingStore",
    "NoOpStagingStore",
    "StagingStore",
    "get_staging_store",
]
