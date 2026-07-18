"""Cloudflare R2 temporary credential vending for pod-scoped access."""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import httpx

R2TempCredentialPermission = Literal[
    "admin-read-write",
    "admin-read-only",
    "object-read-write",
    "object-read-only",
]

DEFAULT_R2_TEMP_CREDENTIAL_TTL_S = 21_600
MAX_R2_TEMP_CREDENTIAL_TTL_S = 604_800
DEFAULT_R2_TEMP_CREDENTIAL_PERMISSION: R2TempCredentialPermission = "object-read-write"
DEFAULT_R2_DEBUG_LOG_PREFIX = "debug-logs/"

_PERMISSIONS: frozenset[str] = frozenset(
    {
        "admin-read-write",
        "admin-read-only",
        "object-read-write",
        "object-read-only",
    }
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

_ACCOUNT_ID_ENV = ("CLOUDFLARE_ACCOUNT_ID", "CF_ACCOUNT_ID", "R2_ACCOUNT_ID")
_API_TOKEN_ENV = (
    "CLOUDFLARE_API_TOKEN",
    "CF_API_TOKEN",
    "R2_TEMP_CREDENTIAL_API_TOKEN",
)
_PARENT_ACCESS_KEY_ENV = (
    "R2_PARENT_ACCESS_KEY_ID",
    "CLOUDFLARE_R2_PARENT_ACCESS_KEY_ID",
    "R2_ACCESS_KEY_ID",
    "R2_ACCESS_KEY",
)
_ENABLED_ENV = ("R2_TEMP_CREDENTIALS_ENABLED", "PITWALL_R2_TEMP_CREDENTIALS_ENABLED")
_REQUIRED_ENV = ("R2_TEMP_CREDENTIALS_REQUIRED", "PITWALL_R2_TEMP_CREDENTIALS_REQUIRED")
_TTL_ENV = (
    "R2_TEMP_CREDENTIAL_TTL_S",
    "R2_TEMP_CREDENTIALS_TTL_S",
    "R2_TEMP_CREDENTIAL_TTL_SECONDS",
    "R2_TEMP_CREDENTIALS_TTL_SECONDS",
    "PITWALL_R2_TEMP_CREDENTIAL_TTL_S",
    "PITWALL_R2_TEMP_CREDENTIALS_TTL_S",
    "PITWALL_R2_TEMP_CREDENTIAL_TTL_SECONDS",
    "PITWALL_R2_TEMP_CREDENTIALS_TTL_SECONDS",
)
_PERMISSION_ENV = (
    "R2_TEMP_CREDENTIAL_PERMISSION",
    "R2_TEMP_CREDENTIALS_PERMISSION",
    "PITWALL_R2_TEMP_CREDENTIAL_PERMISSION",
    "PITWALL_R2_TEMP_CREDENTIALS_PERMISSION",
)
_PREFIXES_ENV = ("R2_TEMP_CREDENTIAL_PREFIXES", "PITWALL_R2_TEMP_CREDENTIAL_PREFIXES")
_OBJECTS_ENV = ("R2_TEMP_CREDENTIAL_OBJECTS", "PITWALL_R2_TEMP_CREDENTIAL_OBJECTS")


class R2TempCredentialError(RuntimeError):
    """Base error for R2 temporary credential vending failures."""


class R2TempCredentialConfigError(R2TempCredentialError):
    """Raised when temporary credential vending is requested but misconfigured."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _zulu(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _non_empty(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _first_env(environ: Mapping[str, str], names: Sequence[str]) -> str | None:
    for name in names:
        value = _non_empty(environ.get(name))
        if value is not None:
            return value
    return None


def _bool_env(environ: Mapping[str, str], names: Sequence[str]) -> bool | None:
    raw = _first_env(environ, names)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    names_display = "/".join(names)
    raise R2TempCredentialConfigError(f"{names_display} must be true or false")


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _validate_ttl(ttl_seconds: int) -> int:
    if ttl_seconds <= 0:
        raise R2TempCredentialConfigError("R2 temporary credential ttl_seconds must be > 0")
    if ttl_seconds > MAX_R2_TEMP_CREDENTIAL_TTL_S:
        raise R2TempCredentialConfigError(
            f"R2 temporary credential ttl_seconds must be <= {MAX_R2_TEMP_CREDENTIAL_TTL_S}"
        )
    return ttl_seconds


def _parse_ttl(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_R2_TEMP_CREDENTIAL_TTL_S
    try:
        ttl_seconds = int(raw)
    except ValueError as exc:
        raise R2TempCredentialConfigError(
            "R2 temporary credential ttl_seconds must be an integer"
        ) from exc
    return _validate_ttl(ttl_seconds)


def _validate_permission(value: str) -> R2TempCredentialPermission:
    normalized = value.strip()
    if normalized not in _PERMISSIONS:
        allowed = ", ".join(sorted(_PERMISSIONS))
        raise R2TempCredentialConfigError(
            f"unsupported R2 temporary credential permission {value!r}; allowed: {allowed}"
        )
    return cast(R2TempCredentialPermission, normalized)


def _default_prefixes(environ: Mapping[str, str]) -> tuple[str, ...]:
    raw_prefix = _non_empty(environ.get("PITWALL_DEBUG_LOG_PREFIX"))
    prefix = raw_prefix or DEFAULT_R2_DEBUG_LOG_PREFIX
    return (prefix.rstrip("/") + "/",)


@dataclass(frozen=True)
class R2TemporaryCredentials:
    """A single short-lived R2 credential set returned by Cloudflare."""

    access_key_id: str
    secret_access_key: str
    session_token: str
    ttl_seconds: int
    bucket: str
    permission: R2TempCredentialPermission
    prefixes: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()
    issued_at: dt.datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if not self.access_key_id.strip():
            raise R2TempCredentialError("temporary R2 access_key_id is empty")
        if not self.secret_access_key.strip():
            raise R2TempCredentialError("temporary R2 secret_access_key is empty")
        if not self.session_token.strip():
            raise R2TempCredentialError("temporary R2 session_token is empty")
        if not self.bucket.strip():
            raise R2TempCredentialError("temporary R2 bucket is empty")
        _validate_ttl(self.ttl_seconds)
        _validate_permission(self.permission)

    @property
    def expires_at(self) -> dt.datetime:
        return self.issued_at.astimezone(dt.UTC) + dt.timedelta(seconds=self.ttl_seconds)

    def as_pod_env(self, *, endpoint: str, bucket: str | None = None) -> dict[str, str]:
        """Return AWS-compatible env vars safe to inject into a worker pod."""

        resolved_bucket = (bucket or self.bucket).strip()
        if not endpoint.strip():
            raise R2TempCredentialConfigError("R2 endpoint is required for pod env")
        if not resolved_bucket:
            raise R2TempCredentialConfigError("R2 bucket is required for pod env")
        return {
            "R2_ENDPOINT": endpoint.strip(),
            "R2_BUCKET_STAGING": resolved_bucket,
            "AWS_ACCESS_KEY_ID": self.access_key_id,
            "AWS_SECRET_ACCESS_KEY": self.secret_access_key,
            "AWS_SESSION_TOKEN": self.session_token,
            "AWS_DEFAULT_REGION": "auto",
            "R2_SESSION_TOKEN": self.session_token,
            "R2_CREDENTIAL_TTL_SECONDS": str(self.ttl_seconds),
            "R2_CREDENTIAL_EXPIRES_AT": _zulu(self.expires_at),
        }


@dataclass(frozen=True)
class R2TempCredentialEnvConfig:
    """Configuration needed to ask Cloudflare for one R2 temp credential."""

    endpoint: str
    bucket: str
    account_id: str
    api_token: str
    parent_access_key_id: str
    ttl_seconds: int = DEFAULT_R2_TEMP_CREDENTIAL_TTL_S
    permission: R2TempCredentialPermission = DEFAULT_R2_TEMP_CREDENTIAL_PERMISSION
    prefixes: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        default_prefixes: Sequence[str] | None = None,
    ) -> R2TempCredentialEnvConfig | None:
        env = os.environ if environ is None else environ
        enabled = _bool_env(env, _ENABLED_ENV)
        required = _bool_env(env, _REQUIRED_ENV) or False
        if enabled is False:
            return None

        endpoint = _first_env(env, ("R2_ENDPOINT",))
        bucket = _first_env(env, ("R2_BUCKET_STAGING", "R2_BUCKET"))
        account_id = _first_env(env, _ACCOUNT_ID_ENV)
        api_token = _first_env(env, _API_TOKEN_ENV)
        parent_access_key_id = _first_env(env, _PARENT_ACCESS_KEY_ENV)

        missing = [
            name
            for name, value in (
                ("R2_ENDPOINT", endpoint),
                ("R2_BUCKET_STAGING", bucket),
                ("CLOUDFLARE_ACCOUNT_ID", account_id),
                ("CLOUDFLARE_API_TOKEN", api_token),
                ("R2_PARENT_ACCESS_KEY_ID", parent_access_key_id),
            )
            if value is None
        ]
        if missing:
            if enabled is True or required:
                raise R2TempCredentialConfigError(
                    "R2 temporary credentials are enabled but missing: " + ", ".join(missing)
                )
            return None

        raw_prefixes = _first_env(env, _PREFIXES_ENV)
        prefixes = _split_csv(raw_prefixes)
        if not prefixes:
            prefixes = tuple(default_prefixes or _default_prefixes(env))

        raw_objects = _first_env(env, _OBJECTS_ENV)
        permission = _validate_permission(
            _first_env(env, _PERMISSION_ENV) or DEFAULT_R2_TEMP_CREDENTIAL_PERMISSION
        )
        return cls(
            endpoint=endpoint or "",
            bucket=bucket or "",
            account_id=account_id or "",
            api_token=api_token or "",
            parent_access_key_id=parent_access_key_id or "",
            ttl_seconds=_parse_ttl(_first_env(env, _TTL_ENV)),
            permission=permission,
            prefixes=prefixes,
            objects=_split_csv(raw_objects),
        )


class CloudflareR2TempCredentialClient:
    """Small synchronous client for Cloudflare's temp-access-credentials API."""

    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        base_url: str = "https://api.cloudflare.com/client/v4",
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not account_id.strip():
            raise R2TempCredentialConfigError("account_id is required")
        if not api_token.strip():
            raise R2TempCredentialConfigError("api_token is required")
        self.account_id = account_id.strip()
        self.api_token = api_token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.transport = transport

    def create(
        self,
        *,
        bucket: str,
        parent_access_key_id: str,
        ttl_seconds: int = DEFAULT_R2_TEMP_CREDENTIAL_TTL_S,
        permission: R2TempCredentialPermission = DEFAULT_R2_TEMP_CREDENTIAL_PERMISSION,
        prefixes: Sequence[str] = (),
        objects: Sequence[str] = (),
        issued_at: dt.datetime | None = None,
    ) -> R2TemporaryCredentials:
        if not bucket.strip():
            raise R2TempCredentialConfigError("bucket is required")
        if not parent_access_key_id.strip():
            raise R2TempCredentialConfigError("parent_access_key_id is required")

        ttl_seconds = _validate_ttl(ttl_seconds)
        permission = _validate_permission(permission)
        body: dict[str, Any] = {
            "bucket": bucket.strip(),
            "parentAccessKeyId": parent_access_key_id.strip(),
            "permission": permission,
            "ttlSeconds": ttl_seconds,
        }
        clean_prefixes = tuple(prefix.strip() for prefix in prefixes if prefix.strip())
        clean_objects = tuple(obj.strip() for obj in objects if obj.strip())
        if clean_prefixes:
            body["prefixes"] = list(clean_prefixes)
        if clean_objects:
            body["objects"] = list(clean_objects)

        path = f"accounts/{self.account_id}/r2/temp-access-credentials"
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_s,
                transport=self.transport,
            ) as client:
                response = client.post(
                    path,
                    headers={
                        "Authorization": f"Bearer {self.api_token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise R2TempCredentialError(
                f"Cloudflare R2 temporary credential request failed: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise R2TempCredentialError(
                "Cloudflare R2 temporary credential request returned "
                f"HTTP {response.status_code}: {response.text}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise R2TempCredentialError(
                "Cloudflare R2 temporary credential response was not JSON"
            ) from exc
        result = payload.get("result") if isinstance(payload, Mapping) else None
        if not payload.get("success") or not isinstance(result, Mapping):
            errors = payload.get("errors") if isinstance(payload, Mapping) else None
            raise R2TempCredentialError(
                "Cloudflare R2 temporary credential response was unsuccessful: "
                f"{errors or payload!r}"
            )

        access_key_id = _non_empty(result.get("accessKeyId"))
        secret_access_key = _non_empty(result.get("secretAccessKey"))
        session_token = _non_empty(result.get("sessionToken"))
        if access_key_id is None or secret_access_key is None or session_token is None:
            raise R2TempCredentialError(
                "Cloudflare R2 temporary credential response omitted required fields"
            )

        return R2TemporaryCredentials(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            session_token=session_token,
            ttl_seconds=ttl_seconds,
            bucket=bucket.strip(),
            permission=permission,
            prefixes=clean_prefixes,
            objects=clean_objects,
            issued_at=issued_at or _utc_now(),
        )


_STS_CREDENTIAL_ENV_KEYS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "R2_SESSION_TOKEN",
    "R2_CREDENTIAL_EXPIRES_AT",
    "R2_CREDENTIAL_TTL_SECONDS",
)


def vend_r2_temp_credential_pod_env(
    *,
    environ: Mapping[str, str] | None = None,
    client: CloudflareR2TempCredentialClient | None = None,
    default_prefixes: Sequence[str] | None = None,
    issued_at: dt.datetime | None = None,
) -> dict[str, str]:
    """Return pod env with temp R2 credentials, or empty env when unconfigured.

    When STS-style environment variables are already present in the environment
    (e.g., injected by the worker parent process), those are returned directly
    without calling the Cloudflare API.
    """

    env = os.environ if environ is None else environ
    sts_env: dict[str, str] = {}
    for key in _STS_CREDENTIAL_ENV_KEYS:
        value = env.get(key)
        if value:
            sts_env[key] = value
    if sts_env:
        return sts_env

    config = R2TempCredentialEnvConfig.from_env(
        environ,
        default_prefixes=default_prefixes,
    )
    if config is None:
        return {}

    credential_client = client or CloudflareR2TempCredentialClient(
        account_id=config.account_id,
        api_token=config.api_token,
    )
    credentials = credential_client.create(
        bucket=config.bucket,
        parent_access_key_id=config.parent_access_key_id,
        ttl_seconds=config.ttl_seconds,
        permission=config.permission,
        prefixes=config.prefixes,
        objects=config.objects,
        issued_at=issued_at,
    )
    return credentials.as_pod_env(endpoint=config.endpoint, bucket=config.bucket)


def mint_r2_temp_credentials(
    *,
    bucket: str,
    parent_access_key_id: str,
    account_id: str,
    api_token: str,
    ttl_seconds: int = DEFAULT_R2_TEMP_CREDENTIAL_TTL_S,
    permission: R2TempCredentialPermission = DEFAULT_R2_TEMP_CREDENTIAL_PERMISSION,
    prefixes: Sequence[str] = (),
    objects: Sequence[str] = (),
    issued_at: dt.datetime | None = None,
    client: CloudflareR2TempCredentialClient | None = None,
) -> R2TemporaryCredentials:
    """Mint a single R2 temporary credential set from Cloudflare.

    This is a standalone factory function that creates temporary R2 credentials
    directly using the Cloudflare API, without requiring env vars or a client instance.
    """

    credential_client = client or CloudflareR2TempCredentialClient(
        account_id=account_id,
        api_token=api_token,
    )
    return credential_client.create(
        bucket=bucket,
        parent_access_key_id=parent_access_key_id,
        ttl_seconds=ttl_seconds,
        permission=permission,
        prefixes=prefixes,
        objects=objects,
        issued_at=issued_at,
    )


__all__ = [
    "CloudflareR2TempCredentialClient",
    "DEFAULT_R2_DEBUG_LOG_PREFIX",
    "DEFAULT_R2_TEMP_CREDENTIAL_PERMISSION",
    "DEFAULT_R2_TEMP_CREDENTIAL_TTL_S",
    "MAX_R2_TEMP_CREDENTIAL_TTL_S",
    "mint_r2_temp_credentials",
    "R2TempCredentialConfigError",
    "R2TempCredentialEnvConfig",
    "R2TempCredentialError",
    "R2TempCredentialPermission",
    "R2TemporaryCredentials",
    "vend_r2_temp_credential_pod_env",
]
