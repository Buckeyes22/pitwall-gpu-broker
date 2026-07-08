"""Pod lease launch assembly for RunPod providers."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pitwall.core.enums import LeaseRenewalPolicy, LeaseState, ProviderType
from pitwall.core.models import Capability, Lease, LeaseEndpoints, LeaseReadiness, Provider
from pitwall.cost.budget_gate import BudgetGate
from pitwall.cost.sync_gate import estimate_cost
from pitwall.db.repository import LeaseRepository, ProviderRepository
from pitwall.runpod_client.pods import (
    ProviderAttachHangRecoveryRequested,
    ProviderFallbackRequested,
    _create_pod_with_fallback,
    create_pod_with_fallback,
)
from pitwall.runpod_client.templates import (
    ensure_template,
    get_image_ref_from_env,
    get_registry_auth_id_from_env,
)
from pitwall.runpod_client.workloads import WorkloadConfig
from pitwall.staging_store import StagingStore, get_staging_store

log = logging.getLogger("pitwall.api.leases.launch")

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PITWALL_IDENTITY_ENV_KEYS = frozenset(
    {
        "PITWALL_CAPABILITY",
        "PITWALL_CAPABILITY_ID",
        "PITWALL_CAPABILITY_NAME",
        "PITWALL_PROVIDER",
        "PITWALL_PROVIDER_ID",
        "PITWALL_PROVIDER_NAME",
        "PITWALL_PROVIDER_TYPE",
        "PITWALL_REQUEST_ID",
    }
)
_PITWALL_STORAGE_CREDENTIAL_ENV_KEYS = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "R2_ACCESS_KEY",
        "R2_SECRET_KEY",
        "R2_SESSION_TOKEN",
        "R2_CREDENTIAL_EXPIRES_AT",
        "R2_CREDENTIAL_TTL_SECONDS",
    }
)
_FORWARDED_PROCESS_ENV_KEYS = (
    "REDIS_URL",
    "LANGFUSE_HOST",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "R2_ENDPOINT",
    "R2_BUCKET_STAGING",
)
ATTACH_HANG_PROVIDER_COOLDOWN = dt.timedelta(minutes=15)


class LaunchConfigError(RuntimeError):
    """Raised when a provider cannot be assembled into a pod launch."""


class InvalidProviderConfig(LaunchConfigError):
    """Raised when provider.config has an invalid launch shape."""


class ProviderNotPodLease(LaunchConfigError):
    """Raised when launch is attempted for a non pod-lease provider."""


class TemplateImageNotConfigured(LaunchConfigError):
    """Raised when no image ref can be resolved for template creation."""


@dataclass(frozen=True)
class LaunchTemplate:
    """Resolved RunPod template information for a pod lease launch."""

    template_id: str
    template_name: str
    image_ref: str
    registry_auth_id: str | None
    container_disk_gb: int
    volume_mount_path: str


@dataclass(frozen=True)
class LeaseLaunchPlan:
    """Template, env, and RunPod workload shape ready for pod creation."""

    template: LaunchTemplate
    env: dict[str, str]
    workload: WorkloadConfig
    network_volume_id: str | None
    data_center_id: str | None
    volume_attach_timeout_s: float | None


def _provider_config(provider: Provider | Any) -> Mapping[str, Any]:
    config = getattr(provider, "config", {})
    return config if isinstance(config, Mapping) else {}


def _required_attr(obj: object, attr: str) -> str:
    value = getattr(obj, attr, None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise InvalidProviderConfig(f"{attr} must be a non-empty string")


def _optional_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _config_str(config: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _optional_str(config.get(key))
        if value is not None:
            return value
    return None


def _config_int(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise InvalidProviderConfig(f"provider.config[{key!r}] must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value)
        except ValueError as exc:
            raise InvalidProviderConfig(f"provider.config[{key!r}] must be an integer") from exc
    else:
        raise InvalidProviderConfig(f"provider.config[{key!r}] must be an integer")
    if parsed <= 0:
        raise InvalidProviderConfig(f"provider.config[{key!r}] must be > 0")
    return parsed


def _config_float(config: Mapping[str, Any], key: str) -> float | None:
    value = config.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidProviderConfig(f"provider.config[{key!r}] must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidProviderConfig(f"provider.config[{key!r}] must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise InvalidProviderConfig(f"provider.config[{key!r}] must be >= 0")
    return parsed


def _config_str_list(config: Mapping[str, Any], *keys: str) -> list[str] | None:
    for key in keys:
        value = config.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
        elif isinstance(value, list | tuple):
            items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        else:
            raise InvalidProviderConfig(
                f"provider.config[{key!r}] must be a string list or comma-separated string"
            )
        if not items:
            raise InvalidProviderConfig(f"provider.config[{key!r}] must not be empty")
        return items
    return None


def _provider_type_value(provider: Provider | Any) -> str:
    provider_type = getattr(provider, "provider_type", None)
    if isinstance(provider_type, ProviderType):
        return provider_type.value
    if isinstance(provider_type, str):
        return provider_type
    value = getattr(provider_type, "value", None)
    if isinstance(value, str):
        return value
    raise InvalidProviderConfig("provider_type must be set")


def _ensure_pod_lease_provider(provider: Provider | Any) -> None:
    provider_type = _provider_type_value(provider)
    if provider_type != ProviderType.POD_LEASE.value:
        provider_id = getattr(provider, "id", "<unknown>")
        raise ProviderNotPodLease(
            f"provider {provider_id!r} has provider_type={provider_type!r}; "
            f"expected {ProviderType.POD_LEASE.value!r}"
        )


def _capability_id(capability: Capability | Any) -> str:
    return _required_attr(capability, "id")


def _capability_name(capability: Capability | Any) -> str:
    return _required_attr(capability, "name")


def _capability_class(capability: Capability | Any) -> str:
    class_value = getattr(capability, "class_", None)
    if class_value is None:
        class_value = getattr(capability, "capability_class", None)
    if isinstance(class_value, str) and class_value.strip():
        return class_value.strip()
    enum_value = getattr(class_value, "value", None)
    if isinstance(enum_value, str) and enum_value.strip():
        return enum_value.strip()
    return _capability_name(capability)


def _provider_id(provider: Provider | Any) -> str:
    return _required_attr(provider, "id")


def _provider_name(provider: Provider | Any) -> str:
    return _required_attr(provider, "name")


def _template_name_for_provider(
    capability: Capability | Any,
    provider: Provider | Any,
) -> str:
    config = _provider_config(provider)
    configured = _config_str(config, "template_name")
    if configured is not None:
        return configured
    return f"pitwall-{_capability_name(capability)}-{_provider_name(provider)}"


def _image_ref_for_provider(provider: Provider | Any) -> str:
    config = _provider_config(provider)
    configured = _config_str(config, "image_ref", "worker_image", "image")
    if configured is not None:
        return configured
    try:
        return get_image_ref_from_env()
    except RuntimeError as exc:
        raise TemplateImageNotConfigured(str(exc)) from exc


def _volume_mount_path(provider: Provider | Any) -> str:
    config = _provider_config(provider)
    return _config_str(config, "volume_mount_path", "volume_mount") or "/workspace"


def _container_disk_gb(provider: Provider | Any) -> int:
    return _config_int(_provider_config(provider), "container_disk_gb", 50)


def _network_volume_id(provider: Provider | Any) -> str | None:
    config = _provider_config(provider)
    configured = _config_str(config, "network_volume_id", "volume_id")
    if configured is not None:
        return configured
    return _optional_str(os.environ.get("RUNPOD_NETWORK_VOLUME_ID"))


def _data_center_id(provider: Provider | Any) -> str | None:
    if _network_volume_id(provider) is None:
        return None
    region = _optional_str(getattr(provider, "region", None))
    if region is not None:
        return region
    config = _provider_config(provider)
    configured = _config_str(config, "data_center_id", "datacenter_id")
    if configured is not None:
        return configured
    return _optional_str(os.environ.get("RUNPOD_DATA_CENTER_ID"))


def _cloud_type(provider: Provider | Any) -> str:
    value = _optional_str(getattr(provider, "cloud_type", None))
    if value is None:
        value = _config_str(_provider_config(provider), "cloud_type")
    if value is not None:
        return value.upper()
    return "SECURE"


def _gpu_type_priority_mode(config: Mapping[str, Any]) -> Literal["custom", "availability"]:
    raw = config.get("gpu_type_priority_mode", config.get("gpu_selection_priority", "custom"))
    if not isinstance(raw, str):
        raise InvalidProviderConfig("provider.config gpu priority mode must be a string")
    normalized = raw.strip().lower()
    if normalized not in {"custom", "availability"}:
        raise InvalidProviderConfig(
            "provider.config gpu priority mode must be 'custom' or 'availability'"
        )
    return "availability" if normalized == "availability" else "custom"


def _data_center_priority_mode(config: Mapping[str, Any]) -> Literal["custom", "availability"]:
    raw = config.get("data_center_priority", "custom")
    if not isinstance(raw, str):
        raise InvalidProviderConfig("provider.config data_center_priority must be a string")
    normalized = raw.strip().lower()
    if normalized not in {"custom", "availability"}:
        raise InvalidProviderConfig(
            "provider.config data_center_priority must be 'custom' or 'availability'"
        )
    return "availability" if normalized == "availability" else "custom"


def _ports_for_workload(provider: Provider | Any) -> str | None:
    ports = _provider_config(provider).get("ports")
    if ports is None:
        return None
    if isinstance(ports, str):
        return ports.strip() or None
    if not isinstance(ports, Mapping):
        raise InvalidProviderConfig("provider.config['ports'] must be a string or mapping")

    rendered: list[str] = []
    for protocol in ("http", "tcp"):
        values = ports.get(protocol)
        if values is None:
            continue
        if isinstance(values, int):
            rendered.append(f"{values}/{protocol}")
            continue
        if isinstance(values, list | tuple):
            for value in values:
                if not isinstance(value, int):
                    raise InvalidProviderConfig(
                        f"provider.config['ports'][{protocol!r}] must contain integers"
                    )
                rendered.append(f"{value}/{protocol}")
            continue
        raise InvalidProviderConfig(
            f"provider.config['ports'][{protocol!r}] must be an integer or integer list"
        )
    return ",".join(rendered) or None


def _workload_config_for_provider(
    capability: Capability | Any,
    provider: Provider | Any,
) -> WorkloadConfig:
    config = _provider_config(provider)
    gpu_types = _config_str_list(config, "gpu_types", "gpu_type_priority")
    if gpu_types is None:
        raise InvalidProviderConfig(
            "provider.config must include 'gpu_types' or 'gpu_type_priority'"
        )
    return WorkloadConfig(
        name=_provider_name(provider),
        capability=_capability_name(capability),
        template_name=_template_name_for_provider(capability, provider),
        gpu_types=gpu_types,
        gpu_count=_config_int(config, "gpu_count", 1),
        container_disk_gb=_config_int(config, "container_disk_gb", 50),
        min_vcpu=_config_int(config, "min_vcpu", 4),
        min_memory_gb=_config_int(config, "min_memory_gb", 16),
        cloud_type=_cloud_type(provider),
        gpu_type_priority=_gpu_type_priority_mode(config),
        data_center_priority=_data_center_priority_mode(config),
        allowed_cuda_versions=_config_str_list(config, "allowed_cuda_versions", "cuda_versions"),
        ports=_ports_for_workload(provider),
    )


def _max_cost_per_hr(provider: Provider | Any) -> float | None:
    config = _provider_config(provider)
    constraints = config.get("constraints")
    sources = [constraints, config] if isinstance(constraints, Mapping) else [config]
    for source in sources:
        for key in ("max_cost_per_hr", "max_cost_per_hour"):
            value = _config_float(source, key)
            if value is not None:
                return value
    return None


def _provider_attach_timeout_s(provider: Provider | Any) -> float | None:
    config = _provider_config(provider)
    constraints = config.get("constraints")
    sources = [constraints, config] if isinstance(constraints, Mapping) else [config]
    for source in sources:
        for key in ("max_attach_hang_s", "volume_attach_timeout_s", "attach_timeout_s"):
            value = _config_float(source, key)
            if value is not None:
                return value
    return None


def _lease_id_for_launch(provider: Provider | Any) -> str:
    """Generate a unique lease ID from provider and a UUID suffix."""
    safe_id = _provider_id(provider).replace("-", "_")
    return f"lease_{safe_id}_{uuid.uuid4().hex[:12]}"


def _expiry_for_lease(provider: Provider | Any, created_at: dt.datetime) -> dt.datetime:
    """Calculate lease expiry time from provider config or default."""
    config = _provider_config(provider)
    ttl_ms = config.get("lease_ttl_ms") or config.get("ttl_ms") or 7200000
    ttl_s = int(ttl_ms) / 1000 if isinstance(ttl_ms, (int, float)) else 7200
    return created_at + dt.timedelta(seconds=ttl_s)


def _planned_endpoints_for_provider(provider: Provider | Any) -> LeaseEndpoints:
    """Construct planned LeaseEndpoints from provider port config."""
    config = _provider_config(provider)
    ports = config.get("ports") or {}
    http_endpoints: dict[str, str] = {}
    tcp_endpoints: dict[str, Any] = {}

    if isinstance(ports, Mapping):
        for protocol in ("http", "tcp"):
            values = ports.get(protocol)
            if values is None:
                continue
            if isinstance(values, int):
                values = [values]
            if isinstance(values, (list, tuple)):
                for port in values:
                    if not isinstance(port, int):
                        continue
                    if protocol == "http":
                        http_endpoints[str(port)] = f"https://{{pod_id}}-{port}.proxy.runpod.net"
                    else:
                        tcp_endpoints[str(port)] = {
                            "host": "{pod_id}.proxy.runpod.net",
                            "port": port,
                        }

    return LeaseEndpoints(http=http_endpoints, tcp=tcp_endpoints)


def _lease_readiness_from_ready_pod(pod: Mapping[str, Any]) -> LeaseReadiness:
    readiness_json = pod.get("readiness")
    if not isinstance(readiness_json, Mapping):
        pod_id = pod.get("id") or "<unknown>"
        raise LaunchConfigError(f"ready pod {pod_id!r} did not include readiness signals")

    readiness = LeaseReadiness.model_validate(dict(readiness_json))
    if not readiness.has_active_signals:
        pod_id = pod.get("id") or "<unknown>"
        raise LaunchConfigError(f"ready pod {pod_id!r} has incomplete readiness signals")
    return readiness


async def _persist_ready_lease(
    pool: Any,
    *,
    lease_id: str,
    ready_pod: Mapping[str, Any],
) -> None:
    if not hasattr(pool, "acquire"):
        return

    lease_repo = LeaseRepository(pool)
    readiness = _lease_readiness_from_ready_pod(ready_pod)
    await lease_repo.update_state(lease_id, LeaseState.WAITING_RUNTIME.value)
    await lease_repo.update_state(lease_id, LeaseState.WAITING_PROBE.value)
    await lease_repo.update_readiness(lease_id, readiness)
    await lease_repo.update_state(lease_id, LeaseState.ACTIVE.value)


async def _upsert_initial_lease(pool: Any, lease: Lease) -> None:
    async with pool.acquire() as conn:
        await conn.fetchrow(
            """
            INSERT INTO pitwall.leases
                (id, provider_id, runpod_pod_id, state, created_at,
                 expires_at, renewal_policy, auto_teardown_on_expiry,
                 endpoints, readiness, cost_accrued_usd, last_health_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb,
                    $11, $12)
            ON CONFLICT (id) DO UPDATE SET
                provider_id = EXCLUDED.provider_id,
                runpod_pod_id = EXCLUDED.runpod_pod_id,
                state = EXCLUDED.state,
                created_at = EXCLUDED.created_at,
                expires_at = EXCLUDED.expires_at,
                renewal_policy = EXCLUDED.renewal_policy,
                auto_teardown_on_expiry = EXCLUDED.auto_teardown_on_expiry,
                endpoints = EXCLUDED.endpoints,
                readiness = EXCLUDED.readiness,
                cost_accrued_usd = EXCLUDED.cost_accrued_usd,
                last_health_at = EXCLUDED.last_health_at,
                terminated_at = NULL,
                terminated_reason = NULL
            RETURNING *
            """,
            lease.id,
            lease.provider_id,
            lease.runpod_pod_id,
            lease.state.value if hasattr(lease.state, "value") else lease.state,
            lease.created_at,
            lease.expires_at,
            lease.renewal_policy.value
            if hasattr(lease.renewal_policy, "value")
            else lease.renewal_policy,
            lease.auto_teardown_on_expiry,
            lease.endpoints.model_dump_json() if lease.endpoints is not None else None,
            lease.readiness.model_dump_json() if lease.readiness is not None else None,
            lease.cost_accrued_usd,
            lease.last_health_at,
        )


def _coerce_env_mapping(raw_env: object, *, source: str) -> dict[str, str]:
    if raw_env is None:
        return {}
    if not isinstance(raw_env, Mapping):
        raise InvalidProviderConfig(f"{source} must be a mapping")
    env: dict[str, str] = {}
    for raw_key, raw_value in raw_env.items():
        if not isinstance(raw_key, str) or not _ENV_KEY_RE.fullmatch(raw_key):
            raise InvalidProviderConfig(f"{source} contains invalid env key {raw_key!r}")
        if raw_key in _PITWALL_IDENTITY_ENV_KEYS:
            raise InvalidProviderConfig(
                f"{source} cannot override Pitwall launch identity key {raw_key!r}"
            )
        if raw_key in _PITWALL_STORAGE_CREDENTIAL_ENV_KEYS:
            raise InvalidProviderConfig(
                f"{source} cannot inject Pitwall-managed storage credential key {raw_key!r}"
            )
        if raw_value is None:
            continue
        env[raw_key] = str(raw_value)
    return env


def _env_for_pod(
    capability: Capability | Any,
    provider: Provider | Any,
    *,
    request_id: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    staging_store: StagingStore | None = None,
) -> dict[str, str]:
    """Return per-launch env overrides injected into the RunPod pod."""

    config = _provider_config(provider)
    env = _coerce_env_mapping(config.get("env_vars"), source="provider.config['env_vars']")

    for key in _FORWARDED_PROCESS_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value

    env.update((staging_store or get_staging_store()).vend_pod_credentials())

    env.update(
        {
            "PITWALL_CAPABILITY": _capability_class(capability),
            "PITWALL_CAPABILITY_ID": _capability_id(capability),
            "PITWALL_CAPABILITY_NAME": _capability_name(capability),
            "PITWALL_PROVIDER": _provider_name(provider),
            "PITWALL_PROVIDER_ID": _provider_id(provider),
            "PITWALL_PROVIDER_NAME": _provider_name(provider),
            "PITWALL_PROVIDER_TYPE": _provider_type_value(provider),
        }
    )
    if request_id is not None and request_id.strip():
        env["PITWALL_REQUEST_ID"] = request_id.strip()

    env.update(_coerce_env_mapping(extra_env, source="extra_env"))
    return env


async def ensure_launch_template(
    pool: Any,
    capability: Capability | Any,
    provider: Provider | Any,
    *,
    api_key: str | None = None,
    graphql_url: str | None = None,
) -> LaunchTemplate:
    """Resolve/create the RunPod template for a pod-lease provider."""

    _ensure_pod_lease_provider(provider)
    image_ref = _image_ref_for_provider(provider)
    template_name = _template_name_for_provider(capability, provider)
    registry_auth_id = get_registry_auth_id_from_env(image_ref)
    container_disk_gb = _container_disk_gb(provider)
    volume_mount_path = _volume_mount_path(provider)
    template_kwargs: dict[str, Any] = {}
    if api_key is not None:
        template_kwargs["api_key"] = api_key
    if graphql_url is not None:
        template_kwargs["graphql_url"] = graphql_url
    template_id = await ensure_template(
        pool,
        image_ref,
        template_name=template_name,
        registry_auth_id=registry_auth_id,
        container_disk_gb=container_disk_gb,
        volume_mount_path=volume_mount_path,
        **template_kwargs,
    )
    return LaunchTemplate(
        template_id=template_id,
        template_name=template_name,
        image_ref=image_ref,
        registry_auth_id=registry_auth_id,
        container_disk_gb=container_disk_gb,
        volume_mount_path=volume_mount_path,
    )


async def prepare_lease_launch(
    pool: Any,
    capability: Capability | Any,
    provider: Provider | Any,
    *,
    request_id: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    staging_store: StagingStore | None = None,
    api_key: str | None = None,
    graphql_url: str | None = None,
) -> LeaseLaunchPlan:
    """Assemble template, env, workload, and placement inputs for pod creation."""

    template = await ensure_launch_template(
        pool,
        capability,
        provider,
        api_key=api_key,
        graphql_url=graphql_url,
    )
    return LeaseLaunchPlan(
        template=template,
        env=_env_for_pod(
            capability,
            provider,
            request_id=request_id,
            extra_env=extra_env,
            staging_store=staging_store,
        ),
        workload=_workload_config_for_provider(capability, provider),
        network_volume_id=_network_volume_id(provider),
        data_center_id=_data_center_id(provider),
        volume_attach_timeout_s=_provider_attach_timeout_s(provider),
    )


def estimate_lease_launch_cost(
    capability: Capability,
    provider: Provider | Any,
    payload: Mapping[str, Any] | None = None,
) -> Decimal:
    """Estimate the budget reservation for a pod-lease launch."""

    return estimate_cost(
        capability=capability,
        provider_cost=_provider_config(provider),
        payload=dict(payload or {}),
    )


async def admit_lease_launch(
    pool: Any,
    capability: Capability,
    provider: Provider | Any,
    *,
    budget_gate: Any | None = None,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Admit a pod-lease launch through the account budget gate."""

    _ensure_pod_lease_provider(provider)
    estimate_usd = estimate_lease_launch_cost(capability, provider, payload)
    gate = budget_gate if budget_gate is not None else BudgetGate(pool)
    return await gate.try_launch(
        capability_id=_capability_id(capability),
        provider_id=_provider_id(provider),
        estimate_usd=estimate_usd,
        workload_type="inference",
        idempotency_key=idempotency_key,
    )


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _set_provider_attach_hang_cooldown(
    pool: Any,
    provider: Provider | Any,
    *,
    now: dt.datetime | None = None,
) -> dt.datetime:
    cooldown_until = (now or _utc_now()) + ATTACH_HANG_PROVIDER_COOLDOWN
    if not hasattr(pool, "acquire"):
        log.warning(
            "provider attach-hang cooldown not persisted because pool is unavailable: "
            "provider=%s cooldown_until=%s",
            _provider_id(provider),
            cooldown_until.isoformat(),
        )
        return cooldown_until

    repo = ProviderRepository(pool)
    updated = await repo.patch(_provider_id(provider), cooldown_until=cooldown_until)
    if updated is None:
        log.warning(
            "provider attach-hang cooldown update found no provider row: provider=%s",
            _provider_id(provider),
        )
    return cooldown_until


def _make_pre_lease_persist_callback(
    *,
    pool: Any,
    loop: asyncio.AbstractEventLoop,
    lease_id: str,
    provider_id: str,
    created_at: dt.datetime,
    expiry: dt.datetime,
    planned_endpoints: LeaseEndpoints | None,
) -> Callable[[dict[str, Any]], None]:
    """Build the ``pre_readiness_callback`` that persists the initial lease row.

    RunPod pod creation runs in a worker thread (``create_pod_with_fallback`` ->
    ``asyncio.to_thread(create_pod_with_fallback_sync, ...)``) and invokes this
    callback from inside that thread, before the readiness wait. Persisting here
    is leak-safety: a crash during the (long) readiness wait still leaves a DB
    record to reconcile and teardown the pod.

    ``pool`` (asyncpg) is bound to ``loop`` — the loop that owns its connections.
    The callback therefore schedules the persist coroutine back onto ``loop``
    rather than running it on a fresh loop, which would raise
    ``ConnectionDoesNotExistError`` (connection belongs to another loop).
    """

    def pre_lease_persist_callback(pod: dict[str, Any]) -> None:
        if not hasattr(pool, "acquire"):
            return
        pod_id = str(pod.get("id")) if pod.get("id") else None
        if not pod_id:
            return

        async def _persist_lease() -> None:
            initial_lease = Lease(
                id=lease_id,
                provider_id=provider_id,
                runpod_pod_id=pod_id,
                state=LeaseState.CREATING,
                created_at=created_at,
                expires_at=expiry,
                renewal_policy=LeaseRenewalPolicy.MANUAL,
                auto_teardown_on_expiry=True,
                endpoints=planned_endpoints,
            )
            await _upsert_initial_lease(pool, initial_lease)

        # The callback fires from a worker thread (create_pod_with_fallback_sync
        # under asyncio.to_thread). asyncio.run() here would spin up a fresh loop
        # and touch a pool bound to ``loop``, raising ConnectionDoesNotExistError.
        # Schedule the persist back onto the owning loop and block until it lands.
        future = asyncio.run_coroutine_threadsafe(_persist_lease(), loop)
        future.result()

    return pre_lease_persist_callback


async def _run_launch_runpod(
    pool: Any,
    capability: Capability,
    provider: Provider | Any,
    *,
    request_id: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    payload: Mapping[str, Any] | None = None,
    budget_gate: Any | None = None,
    idempotency_key: str | None = None,
    dry_run: bool = False,
    api_key: str | None = None,
    graphql_url: str | None = None,
    rest_api_url: str | None = None,
) -> dict[str, Any]:
    workload_id: str | None = None
    if not dry_run:
        workload_id = await admit_lease_launch(
            pool,
            capability,
            provider,
            budget_gate=budget_gate,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    plan = await prepare_lease_launch(
        pool,
        capability,
        provider,
        request_id=request_id,
        extra_env=extra_env,
        api_key=api_key,
        graphql_url=graphql_url,
    )
    response: dict[str, Any] = {
        "backend": "runpod",
        "dry_run": dry_run,
        "capability_id": _capability_id(capability),
        "capability": _capability_name(capability),
        "provider_id": _provider_id(provider),
        "provider": _provider_name(provider),
        "workload_id": workload_id,
        "template_id": plan.template.template_id,
        "template_name": plan.template.template_name,
        "image_ref": plan.template.image_ref,
        "network_volume_id": plan.network_volume_id,
        "data_center_id": plan.data_center_id,
    }
    if dry_run:
        response["pod_id"] = None
        return response

    created_at = dt.datetime.now(dt.UTC)
    lease_id = _lease_id_for_launch(provider)
    expiry = _expiry_for_lease(provider, created_at)
    planned_endpoints = _planned_endpoints_for_provider(provider)

    pre_lease_persist_callback = _make_pre_lease_persist_callback(
        pool=pool,
        loop=asyncio.get_running_loop(),
        lease_id=lease_id,
        provider_id=_provider_id(provider),
        created_at=created_at,
        expiry=expiry,
        planned_endpoints=planned_endpoints,
    )

    pod_name = f"pitwall-{_provider_name(provider)}-{plan.template.template_id[:8]}"
    try:
        create_kwargs: dict[str, Any] = {
            "name": pod_name,
            "template_id": plan.template.template_id,
            "image_name": plan.template.image_ref,
            "workload": plan.workload,
            "env": plan.env,
            "network_volume_id": plan.network_volume_id,
            "data_center_id": plan.data_center_id,
            "max_cost_per_hr": _max_cost_per_hr(provider),
            "volume_attach_timeout_s": plan.volume_attach_timeout_s,
            "pre_readiness_callback": pre_lease_persist_callback,
        }
        if api_key is not None or rest_api_url is not None:
            ready_pod = await _create_pod_with_fallback(
                **create_kwargs,
                api_key=api_key,
                rest_api_url=rest_api_url,
            )
        else:
            ready_pod = await create_pod_with_fallback(**create_kwargs)
    except ProviderAttachHangRecoveryRequested as exc:
        cooldown_until = await _set_provider_attach_hang_cooldown(pool, provider)
        log.warning(
            "pod lease provider attach hang recovered: provider=%s pod=%s "
            "attach_timeout_s=%s cooldown_until=%s reason=%s",
            _provider_id(provider),
            exc.pod_id,
            exc.attach_timeout_s,
            cooldown_until.isoformat(),
            exc,
        )
        response.update(
            {
                "dry_run": False,
                "pod_id": None,
                "lease_id": None,
                "provider_fallback": True,
                "provider_fallback_reason": str(exc),
                "provider_cooldown_until": cooldown_until.isoformat(),
            }
        )
        return response
    except ProviderFallbackRequested as exc:
        log.warning(
            "pod lease provider fallback requested: provider=%s reason=%s",
            _provider_id(provider),
            exc,
        )
        response.update(
            {
                "dry_run": False,
                "pod_id": None,
                "lease_id": None,
                "provider_fallback": True,
                "provider_fallback_reason": str(exc),
            }
        )
        return response

    await _persist_ready_lease(pool, lease_id=lease_id, ready_pod=ready_pod)

    pod_id = str(ready_pod.get("id")) if ready_pod.get("id") else None
    response.update(
        {
            "dry_run": False,
            "pod_id": pod_id,
            "pod_name": ready_pod.get("name"),
            "lease_id": lease_id,
        }
    )
    return response


async def run_launch(
    *,
    pool: Any,
    capability: Capability,
    provider: Provider | Any,
    request_id: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    payload: Mapping[str, Any] | None = None,
    budget_gate: Any | None = None,
    idempotency_key: str | None = None,
    dry_run: bool = False,
    api_key: str | None = None,
    graphql_url: str | None = None,
    rest_api_url: str | None = None,
) -> dict[str, Any]:
    """Launch or dry-run a RunPod pod lease for a resolved provider."""

    log.info(
        "pod lease launch requested: capability=%s provider=%s dry_run=%s",
        _capability_name(capability),
        _provider_name(provider),
        dry_run,
    )
    return await _run_launch_runpod(
        pool,
        capability,
        provider,
        request_id=request_id,
        extra_env=extra_env,
        payload=payload,
        budget_gate=budget_gate,
        idempotency_key=idempotency_key,
        dry_run=dry_run,
        api_key=api_key,
        graphql_url=graphql_url,
        rest_api_url=rest_api_url,
    )


__all__ = [
    "InvalidProviderConfig",
    "LaunchConfigError",
    "LaunchTemplate",
    "LeaseLaunchPlan",
    "ProviderNotPodLease",
    "TemplateImageNotConfigured",
    "admit_lease_launch",
    "ensure_launch_template",
    "estimate_lease_launch_cost",
    "prepare_lease_launch",
    "run_launch",
]
