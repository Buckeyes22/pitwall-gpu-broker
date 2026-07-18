"""Runtime configuration guardrails for Pitwall services.

This module provides strict pydantic settings that validate all environment
variables consumed by Pitwall code. Every env var is explicitly defined
with type and validation, and unknown keys are rejected (extra="forbid").
"""

from __future__ import annotations

import ipaddress
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from pitwall.r2_temp_credentials import (
    DEFAULT_R2_TEMP_CREDENTIAL_PERMISSION,
    DEFAULT_R2_TEMP_CREDENTIAL_TTL_S,
    MAX_R2_TEMP_CREDENTIAL_TTL_S,
    R2TempCredentialPermission,
    _validate_permission,
)

CONFIG_FILE_ENV = "PITWALL_CONFIG_FILE"
DEFAULT_CONFIG_FILE = "pitwall.toml"
UNSAFE_BIND_OVERRIDE_ENV = "PITWALL_UNSAFE_ALLOW_INSECURE_BIND"


class _StrictBase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RunPodConfig(_StrictBase):
    api_key: str = Field(description="Pitwall control-plane access to RunPod")
    rest_api_url: str = Field(
        default="https://rest.runpod.io/v1",
        description="RunPod REST API base URL",
    )
    network_volume_id: str = Field(default="", description="RunPod network volume ID")
    data_center_id: str = Field(default="", description="RunPod data center ID")
    registry_auth_id: str = Field(default="", description="RunPod registry auth ID")
    registry_auth_id_ghcr: str = Field(default="", description="RunPod GHCR registry auth ID")
    registry_auth_id_gitlab: str = Field(default="", description="RunPod GitLab registry auth ID")
    registry_auth_id_docker_hub: str | None = Field(
        default=None, description="RunPod Docker Hub registry auth ID"
    )


class DatabaseConfig(_StrictBase):
    url: str = Field(description="PostgreSQL connection URL")


class RedisConfig(_StrictBase):
    url: str = Field(description="Redis connection URL")


class LangfuseConfig(_StrictBase):
    host: str = Field(default="", description="Langfuse host for trace emission")
    public_key: str = Field(default="", description="Langfuse public key")
    secret_key: str = Field(default="", description="Langfuse secret key")


class R2Config(_StrictBase):
    endpoint: str = Field(default="", description="R2 endpoint for pod log forwarding")
    access_key: str = Field(default="", description="Legacy R2 access key")
    secret_key: str = Field(default="", description="Legacy R2 secret key")
    parent_access_key_id: str = Field(
        default="",
        description="Parent R2 access key ID used only by the control plane for temp creds",
    )
    bucket_staging: str = Field(default="pitwall-staging", description="R2 staging bucket")
    temp_credentials_enabled: str = Field(
        default="auto",
        description="R2 temp credential vending mode: auto, true, or false",
    )
    temp_credentials_required: bool = Field(
        default=False,
        description="Fail pod launch when temp credential vending config is incomplete",
    )
    temp_credential_ttl_s: int = Field(
        default=DEFAULT_R2_TEMP_CREDENTIAL_TTL_S,
        description="R2 temporary credential TTL in seconds",
    )
    temp_credential_permission: R2TempCredentialPermission = Field(
        default=DEFAULT_R2_TEMP_CREDENTIAL_PERMISSION,
        description="R2 temporary credential permission preset",
    )
    temp_credential_prefixes: str = Field(
        default="",
        description="Comma-separated R2 prefixes for temp credential scope",
    )
    temp_credential_objects: str = Field(
        default="",
        description="Comma-separated R2 objects for temp credential scope",
    )


class BudgetConfig(_StrictBase):
    monthly_budget_usd: float = Field(default=50.0, description="Monthly budget hard cap in USD")
    per_request_max_usd: float = Field(default=10.0, description="Per-request cost cap in USD")
    lock_key: int = Field(
        default=5494545452575544,
        description="8-byte big-endian lock key for budget advisory lock",
    )


class LeaseConfig(_StrictBase):
    default_ttl_s: int = Field(default=7200, description="Default lease TTL in seconds (2h)")
    advance_warning_min: str = Field(
        default="15,5", description="T-15 and T-5 pub/sub event triggers"
    )
    volume_attach_timeout_s: int = Field(
        default=300, description="Volume attach timeout in seconds (L7)"
    )
    image_pull_timeout_s: int = Field(default=600, description="Image pull timeout in seconds")


class ResendConfig(_StrictBase):
    api_key: str = Field(default="", description="Resend API key for alert emails")
    sender_email: str = Field(
        default="",
        description="Deprecated legacy sender email; use PITWALL_ALERT_FROM",
    )
    recipient_email: str = Field(
        default="",
        description="Deprecated legacy recipient email; use PITWALL_ALERT_TO",
    )


class AuditConfig(_StrictBase):
    gpu_ids: str = Field(
        default="NVIDIA H100 80GB HBM3,NVIDIA L4,NVIDIA A100 80GB",
        description="Canonical GPU names for audit (comma-separated)",
    )
    cloud_type: Literal["SECURE", "COMMUNITY"] = Field(
        default="SECURE", description="Cloud type used by audit CLI"
    )
    exec_timeout_s: int = Field(default=3600, description="Execution timeout for audit check 5")
    exec_timeout_max_s: int = Field(
        default=7200, description="Max execution timeout for audit check 5"
    )
    queue_time_s: int = Field(default=300, description="Expected queue time for audit check 6")
    startup_timeout_s: int = Field(
        default=600, description="Pod startup timeout for audit check 11"
    )


class TailscaleConfig(_StrictBase):
    ip: str = Field(default="", description="Optional Tailscale integration address")
    webhook_public_url: str = Field(
        default="", description="Tailnet URL for RunPod webhook registration"
    )


class WebhookConfig(_StrictBase):
    receiver_port: int = Field(default=8082, description="Webhook receiver port")


class McpConfig(_StrictBase):
    transport: Literal["stdio"] = Field(default="stdio", description="Local MCP transport mode")


class WorkerConfig(_StrictBase):
    image: str = Field(default="", description="Default worker image for RunPod templates")
    capacity_error_substrings: str = Field(
        default="", description="Comma-separated capacity error substrings"
    )


class PitwallSettings(BaseSettings):
    """Top-level Pitwall settings.

    Strict pydantic-settings model that defines all configuration consumed by
    Pitwall code. Unknown keys are rejected (extra="forbid").
    """

    model_config = SettingsConfigDict(extra="forbid", populate_by_name=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del env_settings, dotenv_settings, file_secret_settings
        return (
            init_settings,
            _PitwallEnvSettingsSource(settings_cls),
            _PitwallTomlSettingsSource(settings_cls),
        )

    runpod_api_key: str = Field(default="", description="Pitwall control-plane access to RunPod")
    runpod_rest_api_url: str = Field(
        default="https://rest.runpod.io/v1",
        description="RunPod REST API base URL",
    )
    runpod_network_volume_id: str = Field(
        default="",
        description="RunPod network volume ID",
    )
    runpod_data_center_id: str = Field(
        default="",
        description="RunPod data center ID",
    )
    runpod_registry_auth_id: str = Field(
        default="",
        description="RunPod registry auth ID",
    )
    runpod_registry_auth_id_ghcr: str = Field(
        default="",
        description="RunPod GHCR registry auth ID",
    )
    runpod_registry_auth_id_gitlab: str = Field(
        default="",
        description="RunPod GitLab registry auth ID",
    )
    runpod_registry_auth_id_docker_hub: str | None = Field(
        default=None,
        description="RunPod Docker Hub registry auth ID",
    )

    database_url: str = Field(default="", description="PostgreSQL connection URL")
    redis_url: str = Field(default="", description="Redis connection URL")

    langfuse_host: str = Field(
        default="",
        description="Langfuse host for trace emission",
    )
    langfuse_public_key: str = Field(
        default="",
        description="Langfuse public key",
    )
    langfuse_secret_key: str = Field(
        default="",
        description="Langfuse secret key",
    )

    r2_endpoint: str = Field(
        default="",
        description="R2 endpoint for pod log forwarding",
    )
    r2_access_key: str = Field(
        default="",
        description="Legacy R2 access key; not forwarded to pods",
    )
    r2_secret_key: str = Field(
        default="",
        description="Legacy R2 secret key; not forwarded to pods",
    )
    r2_parent_access_key_id: str = Field(
        default="",
        description="Parent R2 access key ID used only for temp credential vending",
    )
    r2_bucket_staging: str = Field(
        default="pitwall-staging",
        description="R2 staging bucket",
    )
    r2_temp_credentials_enabled: str = Field(
        default="auto",
        description="R2 temp credential vending mode: auto, true, or false",
    )
    r2_temp_credentials_required: bool = Field(
        default=False,
        description="Fail pod launch when temp credential vending config is incomplete",
    )
    r2_temp_credential_ttl_s: int = Field(
        default=DEFAULT_R2_TEMP_CREDENTIAL_TTL_S,
        description="R2 temporary credential TTL in seconds",
    )
    r2_temp_credential_permission: R2TempCredentialPermission = Field(
        default=DEFAULT_R2_TEMP_CREDENTIAL_PERMISSION,
        description="R2 temporary credential permission preset",
    )
    r2_temp_credential_prefixes: str = Field(
        default="",
        description="Comma-separated R2 prefixes for temp credential scope",
    )
    r2_temp_credential_objects: str = Field(
        default="",
        description="Comma-separated R2 objects for temp credential scope",
    )
    cloudflare_account_id: str = Field(
        default="",
        description="Cloudflare account ID for R2 temporary credential vending",
    )
    cloudflare_api_token: str = Field(
        default="",
        description="Cloudflare API token for R2 temporary credential vending",
    )

    pitwall_admin_secret: str = Field(
        default="",
        description="Enables /v1/admin/*; unset admin routes return 401",
    )
    pitwall_api_token: str = Field(
        default="",
        description=("Optional whole-API bearer token; unset keeps non-admin routes open"),
    )
    pitwall_inbound_rate_limit: str = Field(
        default="",
        description=("Optional inbound API limit as requests/window, e.g. 60/60s; empty disables"),
    )
    pitwall_monthly_budget_usd: float = Field(
        default=50.0,
        description="Monthly budget hard cap in USD",
    )
    pitwall_per_request_max_usd: float = Field(
        default=10.0,
        description="Per-request cost cap in USD",
    )
    pitwall_budget_lock_key: int = Field(
        default=5494545452575544,
        description="8-byte big-endian lock key for budget advisory lock",
    )
    pitwall_budget_breach_kill_mode: Literal["disabled", "shadow", "armed"] = Field(
        default="disabled",
        description=(
            "Budget-breach -> kill-switch escalation: 'disabled' (default, never), "
            "'shadow' (log what it would do, never fire), 'armed' (fire the kill "
            "switch when the budget is exhausted on a circuit-breaker block)"
        ),
    )
    pitwall_budget_breach_kill_headroom_floor_usd: float = Field(
        default=0.0,
        description=(
            "Headroom floor (USD) at/below which an armed budget breach escalates "
            "to the kill switch; 0 = only when the budget is fully exhausted/overrun"
        ),
    )

    pitwall_default_lease_ttl_s: int = Field(
        default=7200,
        description="Default lease TTL in seconds (2h)",
    )
    pitwall_lease_advance_warning_min: str = Field(
        default="15,5",
        description="T-15 and T-5 pub/sub event triggers",
    )
    pitwall_volume_attach_timeout_s: int = Field(
        default=300,
        description="Volume attach timeout in seconds (L7)",
    )
    pitwall_image_pull_timeout_s: int = Field(
        default=600,
        description="Image pull timeout in seconds",
    )
    pitwall_gpu_broker_capacity_error_substrings: str = Field(
        default="",
        description="Comma-separated capacity error substrings",
    )
    pitwall_cloud_worker_image: str = Field(
        default="",
        description="Default worker image for RunPod templates",
    )

    pitwall_audit_gpu_ids: str = Field(
        default="NVIDIA H100 80GB HBM3,NVIDIA L4,NVIDIA A100 80GB",
        description="Canonical GPU names for audit (comma-separated)",
    )
    pitwall_audit_cloud_type: Literal["SECURE", "COMMUNITY"] = Field(
        default="SECURE",
        description="Cloud type used by audit CLI",
    )
    pitwall_audit_exec_timeout_s: int = Field(
        default=3600,
        description="Execution timeout for audit check 5",
    )
    pitwall_audit_exec_timeout_max_s: int = Field(
        default=7200,
        description="Max execution timeout for audit check 5",
    )
    pitwall_audit_queue_time_s: int = Field(
        default=300,
        description="Expected queue time for audit check 6",
    )
    pitwall_audit_startup_timeout_s: int = Field(
        default=600,
        description="Pod startup timeout for audit check 11",
    )

    pitwall_cost_exporter_port: int = Field(
        default=9109,
        description="Prometheus cost exporter port",
    )
    pitwall_webhook_public_url: str = Field(
        default="",
        description="Tailnet URL for RunPod webhook registration",
    )
    pitwall_webhook_receiver_port: int = Field(
        default=8082,
        description="Webhook receiver port",
    )
    pitwall_mcp_transport: Literal["stdio"] = Field(
        default="stdio",
        description="Local MCP transport mode",
    )
    pitwall_tailscale_ip: str = Field(
        default="",
        description="Optional Tailscale integration address",
    )

    pitwall_alert_from: str = Field(
        default="",
        description="Canonical sender email for alert notifications",
    )
    pitwall_alert_to: str = Field(
        default="",
        description="Canonical recipient email for alert notifications",
    )

    resend_api_key: str = Field(
        default="",
        description="Resend API key for alert notifications",
    )
    resend_sender_email: str = Field(
        default="",
        description="Deprecated legacy sender email; use PITWALL_ALERT_FROM",
    )
    resend_budget_alert_email: str = Field(
        default="",
        description="Deprecated legacy recipient email; use PITWALL_ALERT_TO",
    )

    pitwall_embedding_via_pitwall: bool = Field(
        default=False,
        validation_alias="PITWALL_EMBEDDING_VIA_PITWALL",
        description="Route embedding requests through Pitwall instead of direct RunPod",
    )
    pitwall_base_url: str = Field(
        default="",
        validation_alias="PITWALL_BASE_URL",
        description="Base URL for Pitwall server when PITWALL_EMBEDDING_VIA_PITWALL is enabled",
    )


class ConfigFileError(ValueError):
    """Raised when the optional Pitwall config file cannot be loaded."""


_ENV_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "runpod_api_key": ("RUNPOD_API_KEY",),
    "runpod_rest_api_url": ("RUNPOD_REST_API_URL",),
    "runpod_network_volume_id": ("RUNPOD_NETWORK_VOLUME_ID",),
    "runpod_data_center_id": ("RUNPOD_DATA_CENTER_ID",),
    "runpod_registry_auth_id": ("RUNPOD_REGISTRY_AUTH_ID",),
    "runpod_registry_auth_id_ghcr": ("RUNPOD_REGISTRY_AUTH_ID_GHCR",),
    "runpod_registry_auth_id_gitlab": ("RUNPOD_REGISTRY_AUTH_ID_GITLAB",),
    "runpod_registry_auth_id_docker_hub": ("RUNPOD_REGISTRY_AUTH_ID_DOCKER_HUB",),
    "database_url": ("DATABASE_URL",),
    "redis_url": ("REDIS_URL",),
    "langfuse_host": ("LANGFUSE_HOST",),
    "langfuse_public_key": ("LANGFUSE_PUBLIC_KEY",),
    "langfuse_secret_key": ("LANGFUSE_SECRET_KEY",),
    "r2_endpoint": ("R2_ENDPOINT",),
    "r2_access_key": ("R2_ACCESS_KEY",),
    "r2_secret_key": ("R2_SECRET_KEY",),
    "r2_parent_access_key_id": (
        "R2_PARENT_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_PARENT_ACCESS_KEY_ID",
        "R2_ACCESS_KEY_ID",
        "R2_ACCESS_KEY",
    ),
    "r2_bucket_staging": ("R2_BUCKET_STAGING",),
    "r2_temp_credentials_enabled": (
        "R2_TEMP_CREDENTIALS_ENABLED",
        "PITWALL_R2_TEMP_CREDENTIALS_ENABLED",
    ),
    "r2_temp_credentials_required": (
        "R2_TEMP_CREDENTIALS_REQUIRED",
        "PITWALL_R2_TEMP_CREDENTIALS_REQUIRED",
    ),
    "r2_temp_credential_ttl_s": (
        "R2_TEMP_CREDENTIAL_TTL_S",
        "R2_TEMP_CREDENTIALS_TTL_S",
        "R2_TEMP_CREDENTIAL_TTL_SECONDS",
        "R2_TEMP_CREDENTIALS_TTL_SECONDS",
        "PITWALL_R2_TEMP_CREDENTIAL_TTL_S",
        "PITWALL_R2_TEMP_CREDENTIALS_TTL_S",
        "PITWALL_R2_TEMP_CREDENTIAL_TTL_SECONDS",
        "PITWALL_R2_TEMP_CREDENTIALS_TTL_SECONDS",
    ),
    "r2_temp_credential_permission": (
        "R2_TEMP_CREDENTIAL_PERMISSION",
        "R2_TEMP_CREDENTIALS_PERMISSION",
        "PITWALL_R2_TEMP_CREDENTIAL_PERMISSION",
        "PITWALL_R2_TEMP_CREDENTIALS_PERMISSION",
    ),
    "r2_temp_credential_prefixes": (
        "R2_TEMP_CREDENTIAL_PREFIXES",
        "PITWALL_R2_TEMP_CREDENTIAL_PREFIXES",
    ),
    "r2_temp_credential_objects": (
        "R2_TEMP_CREDENTIAL_OBJECTS",
        "PITWALL_R2_TEMP_CREDENTIAL_OBJECTS",
    ),
    "cloudflare_account_id": (
        "CLOUDFLARE_ACCOUNT_ID",
        "CF_ACCOUNT_ID",
        "R2_ACCOUNT_ID",
    ),
    "cloudflare_api_token": (
        "CLOUDFLARE_API_TOKEN",
        "CF_API_TOKEN",
        "R2_TEMP_CREDENTIAL_API_TOKEN",
    ),
    "pitwall_admin_secret": ("PITWALL_ADMIN_SECRET",),
    "pitwall_api_token": ("PITWALL_API_TOKEN",),
    "pitwall_inbound_rate_limit": ("PITWALL_INBOUND_RATE_LIMIT",),
    "pitwall_monthly_budget_usd": ("PITWALL_MONTHLY_BUDGET_USD",),
    "pitwall_per_request_max_usd": ("PITWALL_PER_REQUEST_MAX_USD",),
    "pitwall_budget_lock_key": ("PITWALL_BUDGET_LOCK_KEY",),
    "pitwall_budget_breach_kill_mode": ("PITWALL_BUDGET_BREACH_KILL_MODE",),
    "pitwall_budget_breach_kill_headroom_floor_usd": (
        "PITWALL_BUDGET_BREACH_KILL_HEADROOM_FLOOR_USD",
    ),
    "pitwall_default_lease_ttl_s": ("PITWALL_DEFAULT_LEASE_TTL_S",),
    "pitwall_lease_advance_warning_min": ("PITWALL_LEASE_ADVANCE_WARNING_MIN",),
    "pitwall_volume_attach_timeout_s": ("PITWALL_VOLUME_ATTACH_TIMEOUT_S",),
    "pitwall_image_pull_timeout_s": ("PITWALL_IMAGE_PULL_TIMEOUT_S",),
    "pitwall_gpu_broker_capacity_error_substrings": ("PITWALL_RUNPOD_CAPACITY_ERROR_SUBSTRINGS",),
    "pitwall_cloud_worker_image": ("PITWALL_CLOUD_WORKER_IMAGE",),
    "pitwall_audit_gpu_ids": ("PITWALL_AUDIT_GPU_IDS",),
    "pitwall_audit_cloud_type": ("PITWALL_AUDIT_CLOUD_TYPE",),
    "pitwall_audit_exec_timeout_s": ("PITWALL_AUDIT_EXEC_TIMEOUT_S",),
    "pitwall_audit_exec_timeout_max_s": ("PITWALL_AUDIT_EXEC_TIMEOUT_MAX_S",),
    "pitwall_audit_queue_time_s": ("PITWALL_AUDIT_QUEUE_TIME_S",),
    "pitwall_audit_startup_timeout_s": ("PITWALL_AUDIT_STARTUP_TIMEOUT_S",),
    "pitwall_cost_exporter_port": ("PITWALL_COST_EXPORTER_PORT",),
    "pitwall_webhook_public_url": ("PITWALL_WEBHOOK_PUBLIC_URL",),
    "pitwall_webhook_receiver_port": ("PITWALL_WEBHOOK_RECEIVER_PORT",),
    "pitwall_mcp_transport": ("PITWALL_MCP_TRANSPORT",),
    "pitwall_tailscale_ip": ("PITWALL_TAILSCALE_IP",),
    "pitwall_alert_from": ("PITWALL_ALERT_FROM", "RESEND_SENDER_EMAIL"),
    "pitwall_alert_to": ("PITWALL_ALERT_TO", "RESEND_BUDGET_ALERT_EMAIL"),
    "resend_api_key": ("RESEND_API_KEY",),
    "resend_sender_email": ("RESEND_SENDER_EMAIL",),
    "resend_budget_alert_email": ("RESEND_BUDGET_ALERT_EMAIL",),
    "pitwall_embedding_via_pitwall": ("PITWALL_EMBEDDING_VIA_PITWALL",),
    "pitwall_base_url": ("PITWALL_BASE_URL",),
}


class _PitwallEnvSettingsSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        del field
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return _explicit_env_settings_data(os.environ)


class _PitwallTomlSettingsSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        del field
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        path = resolve_config_file()
        if path is None:
            return {}
        explicit = bool(os.environ.get(CONFIG_FILE_ENV, "").strip())
        if explicit and not path.is_file():
            raise ConfigFileError(
                f"{CONFIG_FILE_ENV} points to a config file that does not exist: {path}"
            )
        if not path.is_file():
            return {}
        if path.suffix.lower() != ".toml":
            raise ConfigFileError(f"Pitwall config files must be TOML (.toml); got {path}")
        try:
            raw_data = TomlConfigSettingsSource(self.settings_cls, toml_file=path)()
        except (
            Exception
        ) as exc:  # reason: any TOML read/parse failure becomes ConfigFileError with path
            raise ConfigFileError(f"could not read Pitwall config file {path}: {exc}") from exc
        return _normalize_config_file_data(raw_data)


def resolve_config_file(
    environ: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> Path | None:
    """Return the optional Pitwall TOML config path, if configured or discoverable."""

    env = os.environ if environ is None else environ
    raw_path = env.get(CONFIG_FILE_ENV, "").strip()
    if raw_path:
        return Path(raw_path).expanduser()
    candidate = (cwd or Path.cwd()) / DEFAULT_CONFIG_FILE
    return candidate if candidate.is_file() else None


def _normalize_config_file_data(raw_data: dict[str, Any]) -> dict[str, Any]:
    key_to_field = _config_file_key_to_field()
    normalized: dict[str, Any] = {}
    for key, value in raw_data.items():
        normalized[key_to_field.get(key, key)] = value
    return normalized


def _config_file_key_to_field() -> dict[str, str]:
    key_to_field: dict[str, str] = {}
    for field_name in PitwallSettings.model_fields:
        key_to_field[field_name] = field_name
        key_to_field[field_name.upper()] = field_name
    for field_name, aliases in _ENV_FIELD_ALIASES.items():
        for alias in aliases:
            key_to_field[alias] = field_name
    return key_to_field


def _get_env(key: str, default: str = "", environ: Mapping[str, str] | None = None) -> str:
    """Get an environment variable with optional default."""
    env = os.environ if environ is None else environ
    return env.get(key, default)


def _get_first_env(
    *keys: str,
    default: str = "",
    environ: Mapping[str, str] | None = None,
) -> str:
    env = os.environ if environ is None else environ
    for key in keys:
        value = _get_env(key, environ=env)
        if value.strip():
            return value
    return default


def _get_bool_env(
    key: str,
    default: bool = False,
    environ: Mapping[str, str] | None = None,
) -> bool:
    raw = _get_env(key, environ=environ)
    if not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be true or false")


def _get_first_validation_alias_env(
    field_name: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    field = PitwallSettings.model_fields[field_name]
    validation_alias = field.validation_alias
    if isinstance(validation_alias, AliasChoices):
        keys = [k for k in validation_alias.choices if isinstance(k, str)]
    elif isinstance(validation_alias, str):
        keys = [validation_alias]
    else:
        return {}
    for key in keys:
        value = _get_env(key, environ=environ)
        if value.strip():
            return {field_name: value}
    return {}


def _explicit_env_settings_data(environ: Mapping[str, str]) -> dict[str, Any]:
    """Return only settings explicitly configured through env vars.

    This preserves the historical env parsing rules while allowing the TOML
    source to sit underneath env values instead of being overwritten by defaults.
    """

    data: dict[str, Any] = {}

    def set_if_present(
        field_name: str,
        env_name: str,
        transform: Any = str,
    ) -> None:
        if env_name in environ:
            data[field_name] = transform(_get_env(env_name, environ=environ))

    def set_first_non_empty(
        field_name: str,
        env_names: tuple[str, ...],
        transform: Any = str,
    ) -> None:
        raw = _get_first_env(*env_names, environ=environ)
        if raw.strip():
            data[field_name] = transform(raw)

    set_if_present("runpod_api_key", "RUNPOD_API_KEY")
    set_if_present("runpod_rest_api_url", "RUNPOD_REST_API_URL")
    set_if_present("runpod_network_volume_id", "RUNPOD_NETWORK_VOLUME_ID")
    set_if_present("runpod_data_center_id", "RUNPOD_DATA_CENTER_ID")
    set_if_present("runpod_registry_auth_id", "RUNPOD_REGISTRY_AUTH_ID")
    set_if_present("runpod_registry_auth_id_ghcr", "RUNPOD_REGISTRY_AUTH_ID_GHCR")
    set_if_present("runpod_registry_auth_id_gitlab", "RUNPOD_REGISTRY_AUTH_ID_GITLAB")
    set_if_present(
        "runpod_registry_auth_id_docker_hub",
        "RUNPOD_REGISTRY_AUTH_ID_DOCKER_HUB",
        lambda raw: raw or None,
    )
    set_if_present("database_url", "DATABASE_URL")
    set_if_present("redis_url", "REDIS_URL")
    set_if_present("langfuse_host", "LANGFUSE_HOST")
    set_if_present("langfuse_public_key", "LANGFUSE_PUBLIC_KEY")
    set_if_present("langfuse_secret_key", "LANGFUSE_SECRET_KEY")
    set_if_present("r2_endpoint", "R2_ENDPOINT")
    set_if_present("r2_access_key", "R2_ACCESS_KEY")
    set_if_present("r2_secret_key", "R2_SECRET_KEY")
    set_first_non_empty("r2_parent_access_key_id", _ENV_FIELD_ALIASES["r2_parent_access_key_id"])
    set_if_present("r2_bucket_staging", "R2_BUCKET_STAGING")
    set_first_non_empty(
        "r2_temp_credentials_enabled",
        _ENV_FIELD_ALIASES["r2_temp_credentials_enabled"],
    )
    if any(name in environ for name in _ENV_FIELD_ALIASES["r2_temp_credentials_required"]):
        data["r2_temp_credentials_required"] = _get_bool_env(
            "R2_TEMP_CREDENTIALS_REQUIRED", environ=environ
        ) or _get_bool_env("PITWALL_R2_TEMP_CREDENTIALS_REQUIRED", environ=environ)
    set_first_non_empty(
        "r2_temp_credential_ttl_s",
        _ENV_FIELD_ALIASES["r2_temp_credential_ttl_s"],
        int,
    )
    set_first_non_empty(
        "r2_temp_credential_permission",
        _ENV_FIELD_ALIASES["r2_temp_credential_permission"],
        _validate_permission,
    )
    set_first_non_empty(
        "r2_temp_credential_prefixes",
        _ENV_FIELD_ALIASES["r2_temp_credential_prefixes"],
    )
    set_first_non_empty(
        "r2_temp_credential_objects",
        _ENV_FIELD_ALIASES["r2_temp_credential_objects"],
    )
    set_first_non_empty("cloudflare_account_id", _ENV_FIELD_ALIASES["cloudflare_account_id"])
    set_first_non_empty("cloudflare_api_token", _ENV_FIELD_ALIASES["cloudflare_api_token"])
    set_if_present("pitwall_admin_secret", "PITWALL_ADMIN_SECRET")
    set_if_present("pitwall_api_token", "PITWALL_API_TOKEN")
    set_if_present("pitwall_inbound_rate_limit", "PITWALL_INBOUND_RATE_LIMIT")
    set_if_present(
        "pitwall_monthly_budget_usd",
        "PITWALL_MONTHLY_BUDGET_USD",
        lambda raw: float(raw or "50.0"),
    )
    set_if_present(
        "pitwall_per_request_max_usd",
        "PITWALL_PER_REQUEST_MAX_USD",
        lambda raw: float(raw or "10.0"),
    )
    set_if_present(
        "pitwall_budget_lock_key",
        "PITWALL_BUDGET_LOCK_KEY",
        lambda raw: int(raw or "5494545452575544"),
    )
    set_if_present(
        "pitwall_budget_breach_kill_mode",
        "PITWALL_BUDGET_BREACH_KILL_MODE",
        lambda raw: raw or "disabled",
    )
    set_if_present(
        "pitwall_budget_breach_kill_headroom_floor_usd",
        "PITWALL_BUDGET_BREACH_KILL_HEADROOM_FLOOR_USD",
        lambda raw: float(raw or "0.0"),
    )
    set_if_present(
        "pitwall_default_lease_ttl_s",
        "PITWALL_DEFAULT_LEASE_TTL_S",
        lambda raw: int(raw or "7200"),
    )
    set_if_present("pitwall_lease_advance_warning_min", "PITWALL_LEASE_ADVANCE_WARNING_MIN")
    set_if_present(
        "pitwall_volume_attach_timeout_s",
        "PITWALL_VOLUME_ATTACH_TIMEOUT_S",
        lambda raw: int(raw or "300"),
    )
    set_if_present(
        "pitwall_image_pull_timeout_s",
        "PITWALL_IMAGE_PULL_TIMEOUT_S",
        lambda raw: int(raw or "600"),
    )
    set_if_present(
        "pitwall_gpu_broker_capacity_error_substrings",
        "PITWALL_RUNPOD_CAPACITY_ERROR_SUBSTRINGS",
    )
    set_if_present("pitwall_cloud_worker_image", "PITWALL_CLOUD_WORKER_IMAGE")
    set_if_present("pitwall_audit_gpu_ids", "PITWALL_AUDIT_GPU_IDS")
    set_if_present(
        "pitwall_audit_cloud_type",
        "PITWALL_AUDIT_CLOUD_TYPE",
        lambda raw: raw or "SECURE",
    )
    set_if_present(
        "pitwall_audit_exec_timeout_s",
        "PITWALL_AUDIT_EXEC_TIMEOUT_S",
        lambda raw: int(raw or "3600"),
    )
    set_if_present(
        "pitwall_audit_exec_timeout_max_s",
        "PITWALL_AUDIT_EXEC_TIMEOUT_MAX_S",
        lambda raw: int(raw or "7200"),
    )
    set_if_present(
        "pitwall_audit_queue_time_s",
        "PITWALL_AUDIT_QUEUE_TIME_S",
        lambda raw: int(raw or "300"),
    )
    set_if_present(
        "pitwall_audit_startup_timeout_s",
        "PITWALL_AUDIT_STARTUP_TIMEOUT_S",
        lambda raw: int(raw or "600"),
    )
    set_if_present(
        "pitwall_cost_exporter_port",
        "PITWALL_COST_EXPORTER_PORT",
        lambda raw: int(raw or "9109"),
    )
    set_if_present("pitwall_webhook_public_url", "PITWALL_WEBHOOK_PUBLIC_URL")
    set_if_present(
        "pitwall_webhook_receiver_port",
        "PITWALL_WEBHOOK_RECEIVER_PORT",
        lambda raw: int(raw or "8082"),
    )
    set_if_present(
        "pitwall_mcp_transport",
        "PITWALL_MCP_TRANSPORT",
        lambda raw: cast(Literal["stdio"], raw),
    )
    set_if_present("pitwall_tailscale_ip", "PITWALL_TAILSCALE_IP")
    set_first_non_empty("pitwall_alert_from", _ENV_FIELD_ALIASES["pitwall_alert_from"])
    set_first_non_empty("pitwall_alert_to", _ENV_FIELD_ALIASES["pitwall_alert_to"])
    set_if_present("resend_api_key", "RESEND_API_KEY")
    set_if_present("resend_sender_email", "RESEND_SENDER_EMAIL")
    set_if_present("resend_budget_alert_email", "RESEND_BUDGET_ALERT_EMAIL")
    data.update(_get_first_validation_alias_env("pitwall_embedding_via_pitwall", environ))
    data.update(_get_first_validation_alias_env("pitwall_base_url", environ))
    return data


def load_settings_from_env() -> PitwallSettings:
    """Load PitwallSettings from env, optional TOML config, and defaults.

    Precedence is direct init/CLI values, then environment variables, then the
    optional TOML file (``PITWALL_CONFIG_FILE`` or ``./pitwall.toml``), then
    model defaults. Required runtime fields are empty strings until
    ``require_runtime_env()`` or ``check_domain_config()`` validates them.
    """

    return PitwallSettings()


def is_loopback_host(host: str) -> bool:
    """Return whether a configured bind host is strictly local."""
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def require_credentials_for_bind(
    service: str,
    host: str,
    required_env: tuple[str, ...],
) -> None:
    """Refuse a non-loopback bind when required credentials are absent.

    The override is intentionally explicit and emits a warning. It exists only
    for isolated development environments and must never appear in release
    Compose or operator examples.
    """
    if is_loopback_host(host):
        return
    missing = tuple(name for name in required_env if not os.environ.get(name, "").strip())
    if not missing:
        return
    if os.environ.get(UNSAFE_BIND_OVERRIDE_ENV) == "1":
        print(
            f"WARNING: {service} is binding to {host} without {', '.join(missing)} "
            f"because {UNSAFE_BIND_OVERRIDE_ENV}=1",
            file=sys.stderr,
        )
        return
    print(
        f"{service} refuses non-loopback bind {host!r}; missing required "
        f"credential configuration: {', '.join(missing)}",
        file=sys.stderr,
    )
    raise SystemExit(os.EX_CONFIG)


_CORE_RUNTIME_ENV = ("RUNPOD_API_KEY", "DATABASE_URL", "REDIS_URL")

_REQUIRED_ENV_BY_SERVICE: dict[str, tuple[str, ...]] = {
    "api": _CORE_RUNTIME_ENV,
    "reconciler": _CORE_RUNTIME_ENV,
    "cost-exporter": ("DATABASE_URL",),
    "mcp": _CORE_RUNTIME_ENV,
    "webhook": (),
}

_RUNTIME_ENV_FIELD_BY_ENV = {
    "RUNPOD_API_KEY": "runpod_api_key",
    "DATABASE_URL": "database_url",
    "REDIS_URL": "redis_url",
}

ConfigIssueLevel = Literal["error", "warning"]


@dataclass(frozen=True)
class ConfigIssue:
    level: ConfigIssueLevel
    code: str
    message: str
    hint: str = ""


@dataclass(frozen=True)
class ConfigCheckResult:
    service: str
    issues: tuple[ConfigIssue, ...]

    @property
    def errors(self) -> tuple[ConfigIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level == "error")

    @property
    def warnings(self) -> tuple[ConfigIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors


def required_runtime_env_vars(service: str) -> tuple[str, ...]:
    """Return the required environment variables for a Pitwall service."""
    normalized = _normalize_service(service)
    return _REQUIRED_ENV_BY_SERVICE.get(normalized, _CORE_RUNTIME_ENV)


def check_domain_config(
    service: str = "api",
    *,
    settings: PitwallSettings | None = None,
) -> ConfigCheckResult:
    """Validate Pitwall's boot-time domain configuration."""

    normalized = _normalize_service(service)
    resolved_settings = settings or load_settings_from_env()
    issues: list[ConfigIssue] = []
    _check_required_runtime_settings(normalized, resolved_settings, issues)
    _check_budget_settings(resolved_settings, issues)
    _check_timeout_settings(resolved_settings, issues)
    _check_embedding_settings(resolved_settings, issues)
    _check_r2_settings(resolved_settings, issues)
    _check_optional_integration_groups(resolved_settings, issues)
    return ConfigCheckResult(service=normalized, issues=tuple(issues))


def format_config_check_result(result: ConfigCheckResult) -> str:
    """Return a human-readable config-check report safe for stderr/stdout."""

    lines: list[str] = []
    for issue in result.errors:
        lines.append(_format_config_issue(issue))
    for issue in result.warnings:
        lines.append(_format_config_issue(issue))
    if not lines:
        lines.append(f"OK: pitwall-gpu-broker config valid for {result.service}")
    return "\n".join(lines)


def format_settings_load_error(exc: ConfigFileError | ValidationError | ValueError) -> str:
    """Return a sanitized settings load error without echoing configured values."""

    return _format_settings_load_error(exc)


def require_runtime_env(service: str) -> None:
    """Exit fail-closed when a service has broken runtime configuration.

    Missing or invalid values are reported by name only; configured secret
    values are never echoed. Empty and whitespace-only values are treated as
    missing.
    """
    try:
        result = check_domain_config(service)
    except (ConfigFileError, ValidationError, ValueError) as exc:
        print(format_settings_load_error(exc), file=sys.stderr)
        raise SystemExit(os.EX_CONFIG) from exc
    if result.warnings:
        for issue in result.warnings:
            print(_format_config_issue(issue), file=sys.stderr)
    if result.errors:
        for issue in result.errors:
            print(_format_config_issue(issue), file=sys.stderr)
        raise SystemExit(os.EX_CONFIG)


def _check_required_runtime_settings(
    service: str,
    settings: PitwallSettings,
    issues: list[ConfigIssue],
) -> None:
    missing = []
    for env_name in required_runtime_env_vars(service):
        field_name = _RUNTIME_ENV_FIELD_BY_ENV[env_name]
        if not _has_runtime_value(getattr(settings, field_name)):
            missing.append(env_name)
    if missing:
        noun = "setting" if len(missing) == 1 else "settings"
        issues.append(
            ConfigIssue(
                level="error",
                code="missing-runtime-config",
                message=(
                    f"pitwall {service} missing required runtime {noun}: {', '.join(missing)}"
                ),
                hint=(
                    "Set the environment variable or the matching lower-snake-case key "
                    f"in {DEFAULT_CONFIG_FILE}."
                ),
            )
        )


def _check_budget_settings(settings: PitwallSettings, issues: list[ConfigIssue]) -> None:
    if settings.pitwall_monthly_budget_usd < 0:
        issues.append(
            ConfigIssue(
                level="error",
                code="invalid-budget",
                message="PITWALL_MONTHLY_BUDGET_USD must be non-negative",
            )
        )
    if settings.pitwall_per_request_max_usd < 0:
        issues.append(
            ConfigIssue(
                level="error",
                code="invalid-budget",
                message="PITWALL_PER_REQUEST_MAX_USD must be non-negative",
            )
        )
    if (
        settings.pitwall_monthly_budget_usd > 0
        and settings.pitwall_per_request_max_usd > settings.pitwall_monthly_budget_usd
    ):
        issues.append(
            ConfigIssue(
                level="warning",
                code="budget-cap-mismatch",
                message=(
                    "PITWALL_PER_REQUEST_MAX_USD is greater than "
                    "PITWALL_MONTHLY_BUDGET_USD; a single request can exhaust the month"
                ),
            )
        )


def _check_timeout_settings(settings: PitwallSettings, issues: list[ConfigIssue]) -> None:
    _require_positive(issues, "PITWALL_DEFAULT_LEASE_TTL_S", settings.pitwall_default_lease_ttl_s)
    _require_positive(
        issues,
        "PITWALL_VOLUME_ATTACH_TIMEOUT_S",
        settings.pitwall_volume_attach_timeout_s,
    )
    _require_positive(issues, "PITWALL_IMAGE_PULL_TIMEOUT_S", settings.pitwall_image_pull_timeout_s)
    _require_positive(issues, "PITWALL_AUDIT_EXEC_TIMEOUT_S", settings.pitwall_audit_exec_timeout_s)
    _require_positive(
        issues,
        "PITWALL_AUDIT_EXEC_TIMEOUT_MAX_S",
        settings.pitwall_audit_exec_timeout_max_s,
    )
    _require_positive(issues, "PITWALL_AUDIT_QUEUE_TIME_S", settings.pitwall_audit_queue_time_s)
    _require_positive(
        issues,
        "PITWALL_AUDIT_STARTUP_TIMEOUT_S",
        settings.pitwall_audit_startup_timeout_s,
    )
    _require_port(issues, "PITWALL_WEBHOOK_RECEIVER_PORT", settings.pitwall_webhook_receiver_port)
    _require_port(issues, "PITWALL_COST_EXPORTER_PORT", settings.pitwall_cost_exporter_port)
    if settings.pitwall_audit_exec_timeout_max_s < settings.pitwall_audit_exec_timeout_s:
        issues.append(
            ConfigIssue(
                level="error",
                code="invalid-timeout",
                message=(
                    "PITWALL_AUDIT_EXEC_TIMEOUT_MAX_S must be greater than or equal to "
                    "PITWALL_AUDIT_EXEC_TIMEOUT_S"
                ),
            )
        )
    if settings.pitwall_image_pull_timeout_s < settings.pitwall_audit_startup_timeout_s:
        issues.append(
            ConfigIssue(
                level="warning",
                code="image-timeout-shorter-than-startup",
                message=(
                    "PITWALL_IMAGE_PULL_TIMEOUT_S is shorter than "
                    "PITWALL_AUDIT_STARTUP_TIMEOUT_S; image pulls may time out first"
                ),
            )
        )


def _check_embedding_settings(settings: PitwallSettings, issues: list[ConfigIssue]) -> None:
    if settings.pitwall_embedding_via_pitwall and not _has_runtime_value(settings.pitwall_base_url):
        issues.append(
            ConfigIssue(
                level="error",
                code="missing-embedding-base-url",
                message="PITWALL_BASE_URL is required when PITWALL_EMBEDDING_VIA_PITWALL=true",
            )
        )


def _check_r2_settings(settings: PitwallSettings, issues: list[ConfigIssue]) -> None:
    mode = settings.r2_temp_credentials_enabled.strip().lower()
    if mode not in {"auto", "true", "false"}:
        issues.append(
            ConfigIssue(
                level="error",
                code="invalid-r2-temp-credentials-mode",
                message="R2_TEMP_CREDENTIALS_ENABLED must be one of: auto, true, false",
            )
        )
        return
    if settings.r2_temp_credential_ttl_s <= 0:
        issues.append(
            ConfigIssue(
                level="error",
                code="invalid-r2-temp-credential-ttl",
                message="R2_TEMP_CREDENTIAL_TTL_S must be greater than 0",
            )
        )
    if settings.r2_temp_credential_ttl_s > MAX_R2_TEMP_CREDENTIAL_TTL_S:
        issues.append(
            ConfigIssue(
                level="error",
                code="invalid-r2-temp-credential-ttl",
                message=(
                    "R2_TEMP_CREDENTIAL_TTL_S must be less than or equal to "
                    f"{MAX_R2_TEMP_CREDENTIAL_TTL_S}"
                ),
            )
        )

    required_for_temp_creds = {
        "R2_ENDPOINT": settings.r2_endpoint,
        "R2_BUCKET_STAGING": settings.r2_bucket_staging,
        "CLOUDFLARE_ACCOUNT_ID": settings.cloudflare_account_id,
        "CLOUDFLARE_API_TOKEN": settings.cloudflare_api_token,
        "R2_PARENT_ACCESS_KEY_ID": settings.r2_parent_access_key_id,
    }
    configured_temp_creds = [
        value
        for key, value in required_for_temp_creds.items()
        if key != "R2_BUCKET_STAGING" and _has_runtime_value(value)
    ]
    missing_temp_creds = [
        key for key, value in required_for_temp_creds.items() if not _has_runtime_value(value)
    ]
    if (mode == "true" or settings.r2_temp_credentials_required) and missing_temp_creds:
        issues.append(
            ConfigIssue(
                level="error",
                code="incomplete-r2-temp-credentials",
                message=(
                    "R2 temporary credentials are required but missing: "
                    f"{', '.join(missing_temp_creds)}"
                ),
            )
        )
    elif mode == "auto" and configured_temp_creds and missing_temp_creds:
        issues.append(
            ConfigIssue(
                level="warning",
                code="partial-r2-temp-credentials",
                message=(
                    "R2 temporary credentials are partially configured; missing "
                    f"{', '.join(missing_temp_creds)}. Pitwall will skip temp credential vending."
                ),
            )
        )

    legacy_cleanup = {
        "R2_ENDPOINT": settings.r2_endpoint,
        "R2_ACCESS_KEY": settings.r2_access_key,
        "R2_SECRET_KEY": settings.r2_secret_key,
        "R2_BUCKET_STAGING": settings.r2_bucket_staging,
    }
    configured_cleanup = [
        value
        for key, value in legacy_cleanup.items()
        if key != "R2_BUCKET_STAGING" and _has_runtime_value(value)
    ]
    missing_cleanup = [
        key for key, value in legacy_cleanup.items() if not _has_runtime_value(value)
    ]
    if configured_cleanup and missing_cleanup:
        issues.append(
            ConfigIssue(
                level="warning",
                code="partial-r2-cleanup",
                message=(
                    "Legacy R2 cleanup is partially configured; missing "
                    f"{', '.join(missing_cleanup)}. Pitwall will skip R2 cleanup."
                ),
            )
        )


def _check_optional_integration_groups(
    settings: PitwallSettings,
    issues: list[ConfigIssue],
) -> None:
    _warn_if_partial(
        issues,
        code="partial-langfuse",
        names_to_values={
            "LANGFUSE_HOST": settings.langfuse_host,
            "LANGFUSE_PUBLIC_KEY": settings.langfuse_public_key,
            "LANGFUSE_SECRET_KEY": settings.langfuse_secret_key,
        },
        message_prefix="Langfuse tracing is partially configured",
    )
    _warn_if_partial(
        issues,
        code="partial-resend-alerts",
        names_to_values={
            "RESEND_API_KEY": settings.resend_api_key,
            "PITWALL_ALERT_FROM": settings.pitwall_alert_from,
            "PITWALL_ALERT_TO": settings.pitwall_alert_to,
        },
        message_prefix="Resend budget alerts are partially configured",
    )
    if bool(settings.pitwall_tailscale_ip.strip()) != bool(
        settings.pitwall_webhook_public_url.strip()
    ):
        issues.append(
            ConfigIssue(
                level="warning",
                code="partial-tailscale-webhook",
                message=(
                    "PITWALL_TAILSCALE_IP and PITWALL_WEBHOOK_PUBLIC_URL are usually "
                    "configured together for tailnet webhook delivery"
                ),
            )
        )


def _require_positive(issues: list[ConfigIssue], name: str, value: int) -> None:
    if value <= 0:
        issues.append(
            ConfigIssue(
                level="error",
                code="invalid-timeout",
                message=f"{name} must be greater than 0",
            )
        )


def _require_port(issues: list[ConfigIssue], name: str, value: int) -> None:
    if value < 1 or value > 65535:
        issues.append(
            ConfigIssue(
                level="error",
                code="invalid-port",
                message=f"{name} must be between 1 and 65535",
            )
        )


def _warn_if_partial(
    issues: list[ConfigIssue],
    *,
    code: str,
    names_to_values: dict[str, str],
    message_prefix: str,
) -> None:
    present = [name for name, value in names_to_values.items() if _has_runtime_value(value)]
    if not present:
        return
    missing = [name for name, value in names_to_values.items() if not _has_runtime_value(value)]
    if missing:
        issues.append(
            ConfigIssue(
                level="warning",
                code=code,
                message=f"{message_prefix}; missing {', '.join(missing)}",
            )
        )


def _format_config_issue(issue: ConfigIssue) -> str:
    prefix = "ERROR" if issue.level == "error" else "WARN"
    text = f"{prefix} [{issue.code}] {issue.message}"
    if issue.hint:
        text = f"{text}. {issue.hint}"
    return text


def _format_settings_load_error(exc: ConfigFileError | ValidationError | ValueError) -> str:
    if isinstance(exc, ValidationError):
        lines = ["ERROR [invalid-settings] Pitwall settings could not be parsed:"]
        for error in exc.errors(include_input=False):
            loc = ".".join(str(part) for part in error["loc"])
            lines.append(f"  - {loc}: {error['msg']}")
        return "\n".join(lines)
    return f"ERROR [invalid-settings] {exc}"


def _normalize_service(service: str) -> str:
    normalized = service.strip().lower().replace("_", "-")
    if normalized.startswith("pitwall-"):
        normalized = normalized.removeprefix("pitwall-")
    return normalized


def _has_runtime_value(value: str | None) -> bool:
    return value is not None and bool(value.strip())


def _exit_config(service: str, missing: Sequence[str]) -> None:
    missing_list = ", ".join(missing)
    service_name = service.strip() or "unknown"
    noun = "variable" if len(missing) == 1 else "variables"
    print(
        f"pitwall {service_name} missing required runtime env {noun}: {missing_list}",
        file=sys.stderr,
    )
    raise SystemExit(os.EX_CONFIG)


@lru_cache(maxsize=1)
def get_settings() -> PitwallSettings:
    """Return cached PitwallSettings instance.

    Subsequent calls return the same object.
    """
    return load_settings_from_env()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m pitwall.config SERVICE", file=sys.stderr)
        return os.EX_USAGE
    require_runtime_env(args[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
