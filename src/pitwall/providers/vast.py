"""Vast.ai provider plugin adapter."""

from __future__ import annotations

import datetime as dt
import json
import re
import shlex
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from pitwall.core.enums import LeaseRenewalPolicy, LeaseState
from pitwall.core.models import Capability, Lease
from pitwall.core.models import Provider as ProviderRecord
from pitwall.cost.estimator import PerSecondPricing, TaggedPricingModel, parse_pricing_model
from pitwall.db.repository import LeaseRepository
from pitwall.providers.interface import (
    ProvisionRequest,
    ProvisionResult,
    ReconcileRequest,
    ReconcileResult,
    ResourceStatus,
    StatusRequest,
    StatusResult,
    TeardownRequest,
    TeardownResult,
)
from pitwall.providers.runpod import SafeProviderUrl

VAST_API_URL = "https://console.vast.ai/api/v0"

_HOUR_SECONDS = Decimal(3600)
_HOURLY_USD_QUANTUM = Decimal("0.000001")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ACTIVE_LEASE_STATE_VALUES = (
    LeaseState.CREATING.value,
    LeaseState.WAITING_RUNTIME.value,
    LeaseState.WAITING_PROBE.value,
    LeaseState.ACTIVE.value,
    LeaseState.STOPPING.value,
)
_TERMINAL_LEASE_STATE_VALUES = (
    LeaseState.STOPPED.value,
    LeaseState.FAILED.value,
    LeaseState.EXPIRED.value,
)
_CREATE_FIELD_KEYS = frozenset(
    {
        "args",
        "args_str",
        "cancel_unavail",
        "disk",
        "env",
        "extra",
        "image",
        "image_login",
        "jupyter_dir",
        "jupyter_lab",
        "label",
        "lang_utf8",
        "login",
        "onstart",
        "onstart_cmd",
        "price",
        "python_utf8",
        "runtype",
        "target_state",
        "template_hash_id",
        "user",
        "vm",
    }
)
_PROVISIONING_STATUSES = frozenset(
    {
        "",
        "creating",
        "initializing",
        "loading",
        "pending",
        "provisioning",
        "starting",
    }
)
_RUNNING_STATUSES = frozenset({"ready", "running"})
_TERMINATED_STATUSES = frozenset(
    {
        "deleted",
        "destroyed",
        "exited",
        "offline",
        "stopped",
        "terminated",
    }
)
_FAILED_STATUSES = frozenset({"error", "failed", "unhealthy", "unknown"})
_PREEMPTED_MARKERS = frozenset(
    {
        "evicted",
        "interrupted",
        "outbid",
        "preempted",
        "preempted_by_bid",
    }
)


class VastCredentials(BaseModel):
    """Credentials required for Vast.ai provider operations."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    api_key: SecretStr = Field(min_length=1)
    vast_api_url: SafeProviderUrl = VAST_API_URL
    client_id: str = Field(default="me", min_length=1)

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("api_key must be non-empty")
        return value


class VastProviderError(RuntimeError):
    """Raised when the Vast provider cannot complete an operation."""


class VastProvider:
    """Provider plugin backed by Vast.ai's REST API."""

    id = "vast"
    name = "Vast.ai"
    credential_schema = VastCredentials

    def __init__(
        self,
        *,
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._transport = transport

    def pricing_model(
        self,
        capability: Capability,
        provider_record: ProviderRecord,
    ) -> TaggedPricingModel:
        cost = _cost_mapping(provider_record)
        hourly_rate = _first_present(
            cost,
            "price_per_hour",
            "rate_per_hour",
            "dph_total",
            "on_demand_price_per_hour",
        )
        hourly_bid = _first_present(
            cost,
            "bid_price_per_hour",
            "bid_per_hour",
            "min_bid",
            "spot_price_per_hour",
        )
        if hourly_rate is not None:
            return PerSecondPricing(
                rate_per_second=_hourly_usd_to_per_second(hourly_rate, "price_per_hour"),
                bid_rate_per_second=(
                    _hourly_usd_to_per_second(hourly_bid, "bid_price_per_hour")
                    if hourly_bid is not None
                    else None
                ),
            )
        return parse_pricing_model(provider_record, cost_mode=capability.cost_mode)

    async def provision(self, request: ProvisionRequest) -> ProvisionResult:
        credentials = _vast_credentials(request.credentials)
        config = _config_mapping(request.provider_record)
        payload = dict(request.payload)
        pricing = self.pricing_model(request.capability, request.provider_record)
        ask_id = _offer_id(config, payload)
        body = _create_instance_body(
            config,
            payload,
            provider_id=request.provider_record.id,
            request_id=request.request_id,
            extra_env=request.extra_env,
            pricing=pricing,
        )
        if request.dry_run:
            return ProvisionResult(
                provider_id=request.provider_record.id,
                external_id=None,
                lease_id=None,
                raw={
                    "backend": self.id,
                    "dry_run": True,
                    "ask_id": ask_id,
                    "create": _json_safe(body),
                },
            )

        workload_id = await _admit_budget(request, pricing)
        raw = await self._json_request(
            credentials,
            "PUT",
            f"/asks/{ask_id}/",
            json_body=body,
        )
        external_id = _created_instance_id(raw)
        if external_id is None:
            raise VastProviderError(
                "Vast create succeeded but response did not include an external id"
            )
        lease_id = await _persist_created_lease(
            request.context.pool,
            provider_record=request.provider_record,
            external_id=external_id,
            now=request.context.now,
        )
        result_raw = dict(raw)
        if workload_id is not None:
            result_raw["workload_id"] = workload_id
        return ProvisionResult(
            provider_id=request.provider_record.id,
            external_id=external_id,
            lease_id=lease_id,
            raw=result_raw,
        )

    async def status(self, request: StatusRequest) -> StatusResult:
        credentials = _vast_credentials(request.credentials)
        response = await self._request(
            credentials,
            "GET",
            f"/instances/{_resource_id(request.external_id)}/",
        )
        if response.status_code == 404:
            return StatusResult(
                provider_id=request.provider_record.id,
                external_id=request.external_id,
                status=ResourceStatus.TERMINATED,
                raw={},
            )
        _raise_for_status(response)
        data = _response_payload(response)
        instance = _instance_from_payload(data, request.external_id)
        if instance is None:
            return StatusResult(
                provider_id=request.provider_record.id,
                external_id=request.external_id,
                status=ResourceStatus.UNKNOWN,
                raw=_raw_object(data),
            )
        raw = _annotated_instance(instance)
        return StatusResult(
            provider_id=request.provider_record.id,
            external_id=request.external_id,
            status=_resource_status(raw),
            raw=raw,
        )

    async def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        resources: list[dict[str, Any]]
        if request.external_ids:
            resources = []
            for external_id in request.external_ids:
                status = await self.status(
                    StatusRequest(
                        context=request.context,
                        provider_record=request.provider_record,
                        credentials=request.credentials,
                        external_id=external_id,
                    )
                )
                resource = dict(status.raw)
                if "id" not in resource:
                    resource["id"] = external_id
                resources.append(resource)
        else:
            credentials = _vast_credentials(request.credentials)
            raw = await self._json_request(credentials, "GET", "/instances/")
            resources = [_annotated_instance(item) for item in _instances_from_payload(raw)]

        updated = 0
        for resource in resources:
            resource_external_id = _instance_id(resource)
            if resource_external_id is None or not _is_preempted(resource):
                continue
            updated += await _mark_preempted_failed(
                request.context.pool,
                provider_id=request.provider_record.id,
                external_id=resource_external_id,
                now=request.context.now,
            )

        return ReconcileResult(
            provider_id=request.provider_record.id,
            checked=len(resources),
            updated=updated,
            raw={"resources": resources},
        )

    async def teardown(self, request: TeardownRequest) -> TeardownResult:
        credentials = _vast_credentials(request.credentials)
        external_id = await _external_id_for_lease(request.context.pool, request.lease_id)
        if external_id is None:
            raise VastProviderError(
                f"Vast teardown requires a persisted external id for lease {request.lease_id!r}"
            )

        response = await self._request(
            credentials,
            "DELETE",
            f"/instances/{_resource_id(external_id)}/",
        )
        if response.status_code == 404:
            raw: dict[str, Any] = {"success": True, "already_absent": True}
        else:
            _raise_for_status(response)
            raw_payload = _response_payload(response)
            raw = _raw_object(raw_payload)

        updated = await _close_lease(
            request.context.pool,
            lease_id=request.lease_id,
            terminal_state=request.terminal_state,
            reason=request.reason,
            now=request.context.now,
        )
        if updated:
            raw["lease_updated"] = updated
        return TeardownResult(
            provider_id=request.provider_record.id,
            lease_id=request.lease_id,
            external_id=external_id,
            raw=raw,
        )

    async def _request(
        self,
        credentials: VastCredentials,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {credentials.api_key.get_secret_value()}"}
        content: str | None = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            content = _json_dumps(json_body)
        async with httpx.AsyncClient(
            base_url=credentials.vast_api_url,
            timeout=self._timeout_s,
            transport=self._transport,
        ) as client:
            return await client.request(method, path, headers=headers, content=content)

    async def _json_request(
        self,
        credentials: VastCredentials,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._request(credentials, method, path, json_body=json_body)
        _raise_for_status(response)
        return _raw_object(_response_payload(response))


async def _admit_budget(
    request: ProvisionRequest,
    pricing: TaggedPricingModel,
) -> str | None:
    if request.budget_gate is None:
        return None
    estimate_usd = pricing.upper_bound(request.capability, dict(request.payload))
    launched = await request.budget_gate.try_launch(
        capability_id=request.capability.id,
        provider_id=request.provider_record.id,
        estimate_usd=estimate_usd,
        workload_type="inference",
        idempotency_key=request.idempotency_key,
    )
    return str(launched)


async def _persist_created_lease(
    pool: Any,
    *,
    provider_record: ProviderRecord,
    external_id: str | None,
    now: dt.datetime | None,
) -> str | None:
    if external_id is None or not _has_acquire(pool):
        return None
    created_at = _utc_now(now)
    lease_id = _lease_id_for_provider(provider_record.id)
    lease = Lease(
        id=lease_id,
        provider_id=provider_record.id,
        runpod_pod_id=external_id,
        state=LeaseState.CREATING,
        created_at=created_at,
        expires_at=_expiry_for_lease(provider_record, created_at),
        renewal_policy=LeaseRenewalPolicy.MANUAL,
        auto_teardown_on_expiry=True,
    )
    await LeaseRepository(pool).create(lease)
    return lease_id


async def _mark_preempted_failed(
    pool: Any,
    *,
    provider_id: str,
    external_id: str,
    now: dt.datetime | None,
) -> int:
    if not _has_acquire(pool):
        return 0
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE pitwall.leases
            SET state = $1,
                terminated_at = $2,
                terminated_reason = $3
            WHERE provider_id = $4
              AND runpod_pod_id = $5
              AND state = ANY($6::text[])
            """,
            LeaseState.FAILED.value,
            _utc_now(now),
            "vast_preempted",
            provider_id,
            external_id,
            list(_ACTIVE_LEASE_STATE_VALUES),
        )
    return _rows_affected(result)


async def _external_id_for_lease(pool: Any, lease_id: str) -> str | None:
    if not _has_acquire(pool):
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT runpod_pod_id FROM pitwall.leases WHERE id = $1",
            lease_id,
        )
    if not isinstance(row, Mapping):
        return None
    return _optional_string(row.get("runpod_pod_id"))


async def _close_lease(
    pool: Any,
    *,
    lease_id: str,
    terminal_state: LeaseState | str,
    reason: str | None,
    now: dt.datetime | None,
) -> int:
    if not _has_acquire(pool):
        return 0
    state = _lease_state_value(terminal_state)
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE pitwall.leases
            SET state = $1,
                terminated_at = $2,
                terminated_reason = $3
            WHERE id = $4
              AND state <> ALL($5::text[])
            """,
            state,
            _utc_now(now),
            _teardown_reason(reason, state),
            lease_id,
            list(_TERMINAL_LEASE_STATE_VALUES),
        )
    return _rows_affected(result)


def _create_instance_body(
    config: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    provider_id: str,
    request_id: str | None,
    extra_env: Mapping[str, str] | None,
    pricing: TaggedPricingModel,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    body.update(_mapping_value(config.get("create")))
    body.update(_mapping_value(payload.get("vast_create")))
    body.update(_mapping_value(payload.get("instance")))
    body.update(_mapping_value(payload.get("create")))

    for key in _CREATE_FIELD_KEYS:
        if key in config and key not in body:
            body[key] = config[key]
        if key in payload:
            body[key] = payload[key]

    if "label" not in body:
        body["label"] = _default_label(provider_id, request_id)
    if "price" not in body:
        bid_price = _bid_price_per_hour(config, pricing)
        if bid_price is not None:
            body["price"] = bid_price.quantize(_HOURLY_USD_QUANTUM)

    env_flags = _env_flags(extra_env)
    if env_flags:
        configured_env = body.get("env")
        if isinstance(configured_env, str) and configured_env.strip():
            body["env"] = f"{configured_env.strip()} {env_flags}"
        else:
            body["env"] = env_flags

    if not _optional_string(body.get("image")) and not _optional_string(
        body.get("template_hash_id")
    ):
        raise VastProviderError("Vast provision requires create.image or create.template_hash_id")
    return body


def _offer_id(config: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    raw = _first_present(payload, "ask_id", "offer_id")
    if raw is None:
        raw = _first_present(config, "ask_id", "offer_id")
    value = _optional_string(raw)
    if value is None:
        raise VastProviderError("Vast provision requires ask_id or offer_id")
    if not value.isdigit():
        raise VastProviderError("Vast ask_id/offer_id must contain only digits")
    return value


def _bid_price_per_hour(
    config: Mapping[str, Any],
    pricing: TaggedPricingModel,
) -> Decimal | None:
    cost = _mapping_value(config.get("cost"))
    raw = _first_present(
        cost,
        "bid_price_per_hour",
        "bid_per_hour",
        "min_bid",
        "spot_price_per_hour",
    )
    if raw is not None:
        return _non_negative_decimal(raw, "bid_price_per_hour")
    if isinstance(pricing, PerSecondPricing) and pricing.bid_rate_per_second is not None:
        return pricing.bid_rate_per_second * _HOUR_SECONDS
    return None


def _cost_mapping(provider_record: ProviderRecord) -> Mapping[str, Any]:
    config = _config_mapping(provider_record)
    cost = config.get("cost")
    if isinstance(cost, Mapping):
        return cost
    return config


def _config_mapping(provider_record: ProviderRecord) -> Mapping[str, Any]:
    config = provider_record.config
    return config if isinstance(config, Mapping) else {}


def _instances_from_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    instances = payload.get("instances")
    if isinstance(instances, list):
        return [dict(item) for item in instances if isinstance(item, Mapping)]
    if isinstance(instances, Mapping):
        return [dict(instances)]
    if _looks_like_instance(payload):
        return [dict(payload)]
    return []


def _instance_from_payload(payload: object, external_id: str) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    instances = payload.get("instances")
    if isinstance(instances, Mapping):
        return dict(instances)
    if isinstance(instances, list):
        for item in instances:
            if not isinstance(item, Mapping):
                continue
            item_id = _instance_id(item)
            if item_id == external_id:
                return dict(item)
        return None
    if _looks_like_instance(payload):
        return dict(payload)
    return None


def _annotated_instance(instance: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(instance)
    if _is_preempted(raw):
        raw["pitwall_preempted"] = True
        raw["pitwall_safe_state"] = LeaseState.FAILED.value
    return raw


def _resource_status(instance: Mapping[str, Any]) -> ResourceStatus:
    status = _status_text(instance)
    if _is_preempted(instance):
        return ResourceStatus.FAILED
    if status in _RUNNING_STATUSES:
        return ResourceStatus.RUNNING
    if status in _PROVISIONING_STATUSES:
        return ResourceStatus.PROVISIONING
    if status in _TERMINATED_STATUSES:
        return ResourceStatus.TERMINATED
    if status in _FAILED_STATUSES:
        return ResourceStatus.FAILED
    return ResourceStatus.UNKNOWN


def _is_preempted(instance: Mapping[str, Any]) -> bool:
    values = (
        _status_text(instance),
        _normalized_text(instance.get("status_msg")),
        _normalized_text(instance.get("status_message")),
    )
    return any(marker in value for marker in _PREEMPTED_MARKERS for value in values)


def _status_text(instance: Mapping[str, Any]) -> str:
    for key in ("actual_status", "cur_state", "next_state", "status", "state", "intended_status"):
        value = _normalized_text(instance.get(key))
        if value:
            return value
    return ""


def _instance_id(instance: Mapping[str, Any]) -> str | None:
    for key in ("id", "new_contract", "contract_id", "instance_id"):
        value = _optional_string(instance.get(key))
        if value is not None:
            return value
    return None


def _created_instance_id(payload: Mapping[str, Any]) -> str | None:
    for key in ("new_contract", "contract_id", "instance_id", "id"):
        value = _optional_string(payload.get(key))
        if value is not None:
            return value
    return None


def _looks_like_instance(payload: Mapping[str, Any]) -> bool:
    return any(key in payload for key in ("actual_status", "cur_state", "status", "state"))


def _response_payload(response: httpx.Response) -> object:
    if not response.content:
        return {}
    loaded: object = json.loads(response.text, parse_float=Decimal)
    return loaded


def _raw_object(payload: object) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        return dict(payload)
    return {"data": payload}


def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise VastProviderError(str(exc)) from exc


def _json_dumps(value: Mapping[str, Any]) -> str:
    return _json_value(value)


def _json_value(value: object) -> str:
    if isinstance(value, Decimal):
        return format(_finite_decimal(value, "json decimal"), "f")
    if isinstance(value, Mapping):
        items = (
            f"{json.dumps(str(key), ensure_ascii=True)}:{_json_value(item)}"
            for key, item in value.items()
        )
        return "{" + ",".join(items) + "}"
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return "[" + ",".join(_json_value(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_json_safe(item) for item in value]
    return value


def _hourly_usd_to_per_second(raw_value: object, name: str) -> Decimal:
    return _non_negative_decimal(raw_value, name) / _HOUR_SECONDS


def _non_negative_decimal(raw_value: object, name: str) -> Decimal:
    if isinstance(raw_value, bool):
        raise ValueError(f"{name} must be a decimal value")
    try:
        value = Decimal(str(raw_value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal value") from exc
    return _finite_decimal(value, name)


def _finite_decimal(value: Decimal, name: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _first_present(mapping: Mapping[str, Any], *keys: str) -> object | None:
    for key in keys:
        value: object = mapping.get(key)
        if value is not None:
            return value
    return None


def _mapping_value(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int):
        return str(value)
    return None


def _normalized_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    return ""


def _env_flags(extra_env: Mapping[str, str] | None) -> str:
    if not extra_env:
        return ""
    flags: list[str] = []
    for key, value in extra_env.items():
        if not _ENV_KEY_RE.fullmatch(key):
            raise VastProviderError(f"extra_env contains invalid env key {key!r}")
        flags.append(f"-e {key}={shlex.quote(str(value))}")
    return " ".join(flags)


def _default_label(provider_id: str, request_id: str | None) -> str:
    if request_id is not None and request_id.strip():
        suffix = request_id.strip()
    else:
        suffix = uuid.uuid4().hex[:12]
    return f"pitwall-{provider_id}-{suffix}"


def _lease_id_for_provider(provider_id: str) -> str:
    safe_provider_id = provider_id.replace("-", "_")
    return f"lease_{safe_provider_id}_{uuid.uuid4().hex[:12]}"


def _expiry_for_lease(provider_record: ProviderRecord, created_at: dt.datetime) -> dt.datetime:
    config = _config_mapping(provider_record)
    raw_ttl_ms = config.get("lease_ttl_ms", config.get("ttl_ms", 7_200_000))
    ttl_ms = _positive_int(raw_ttl_ms, "lease_ttl_ms")
    return created_at + dt.timedelta(milliseconds=ttl_ms)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be a positive integer") from exc
    else:
        raise ValueError(f"{name} must be a positive integer")
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _utc_now(value: dt.datetime | None) -> dt.datetime:
    observed = dt.datetime.now(dt.UTC) if value is None else value
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("provider operation time must include timezone information")
    return observed.astimezone(dt.UTC)


def _lease_state_value(state: LeaseState | str) -> str:
    value = state.value if isinstance(state, LeaseState) else str(state)
    LeaseState(value)
    return value


def _teardown_reason(reason: str | None, state: str) -> str:
    if reason is not None and reason.strip():
        return reason.strip()
    if state == LeaseState.EXPIRED.value:
        return "lease_expired"
    if state == LeaseState.FAILED.value:
        return "vast_failed"
    return "operator_stop"


def _rows_affected(result: object) -> int:
    if not isinstance(result, str):
        return 0
    parts = result.strip().split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


def _has_acquire(pool: Any) -> bool:
    acquire = getattr(pool, "acquire", None)
    return callable(acquire)


def _resource_id(value: str) -> str:
    stripped = value.strip()
    if not stripped or "/" in stripped:
        raise VastProviderError("Vast resource id must be non-empty and contain no slashes")
    return stripped


def _vast_credentials(credentials: BaseModel) -> VastCredentials:
    if isinstance(credentials, VastCredentials):
        return credentials
    return VastCredentials.model_validate(credentials)


__all__ = [
    "VAST_API_URL",
    "VastCredentials",
    "VastProvider",
    "VastProviderError",
]
