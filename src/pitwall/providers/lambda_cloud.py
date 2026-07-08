"""Lambda Cloud provider plugin adapter."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from pitwall.core.enums import LeaseRenewalPolicy, LeaseState
from pitwall.core.models import Capability, Lease
from pitwall.core.models import Provider as ProviderRecord
from pitwall.cost.estimator import PerVmSecondPricing, TaggedPricingModel, parse_pricing_model
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

LAMBDA_CLOUD_API_URL = "https://cloud.lambda.ai/api/v1"

_HOUR_SECONDS = Decimal(3600)
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
_LAUNCH_FIELD_KEYS = frozenset(
    {
        "file_system_mounts",
        "file_system_names",
        "firewall_rulesets",
        "hostname",
        "image",
        "instance_type_name",
        "name",
        "quantity",
        "region_name",
        "ssh_key_names",
        "tags",
        "user_data",
    }
)
_PROVISIONING_STATUSES = frozenset(
    {"", "booting", "creating", "launching", "pending", "provisioning", "starting"}
)
_RUNNING_STATUSES = frozenset({"active", "ready", "running"})
_TERMINATED_STATUSES = frozenset({"deleted", "terminated", "terminating"})
_FAILED_STATUSES = frozenset({"error", "failed", "preempted", "unhealthy"})
_PREEMPTED_STATUSES = frozenset({"preempted"})


class LambdaCloudCredentials(BaseModel):
    """Credentials required for Lambda Cloud provider operations."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    api_key: SecretStr = Field(min_length=1)
    lambda_api_url: SafeProviderUrl = LAMBDA_CLOUD_API_URL

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("api_key must be non-empty")
        return value


class LambdaCloudProviderError(RuntimeError):
    """Raised when the Lambda Cloud provider cannot complete an operation."""


class LambdaCloudProvider:
    """Provider plugin backed by Lambda Cloud's REST API."""

    id = "lambda_cloud"
    name = "Lambda Cloud"
    credential_schema = LambdaCloudCredentials

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
        rate_per_second = _first_present(
            cost,
            "rate_per_second",
            "per_vm_second",
            "price_per_second",
            "price_usd_per_second",
        )
        if rate_per_second is not None:
            return PerVmSecondPricing(
                rate_per_second=_non_negative_decimal(rate_per_second, "rate_per_second")
            )

        hourly_rate = _first_present(
            cost,
            "price_per_hour",
            "rate_per_hour",
            "price_usd_per_hour",
        )
        if hourly_rate is not None:
            return PerVmSecondPricing(
                rate_per_second=_hourly_usd_to_per_second(hourly_rate, "price_per_hour")
            )

        pricing = parse_pricing_model(provider_record, cost_mode=capability.cost_mode)
        if not isinstance(pricing, PerVmSecondPricing):
            raise LambdaCloudProviderError("Lambda Cloud provider requires per_vm_second pricing")
        return pricing

    async def provision(self, request: ProvisionRequest) -> ProvisionResult:
        credentials = _lambda_cloud_credentials(request.credentials)
        pricing = self.pricing_model(request.capability, request.provider_record)
        body = _launch_instance_body(
            request.provider_record,
            request.payload,
            request_id=request.request_id,
        )
        if request.dry_run:
            return ProvisionResult(
                provider_id=request.provider_record.id,
                external_id=None,
                lease_id=None,
                raw={
                    "backend": self.id,
                    "dry_run": True,
                    "launch": _json_safe(body),
                },
            )

        workload_id = await _admit_budget(request, pricing)
        raw = await self._json_request(
            credentials,
            "POST",
            "/instance-operations/launch",
            json_body=body,
        )
        external_id = _created_instance_id(raw)
        if external_id is None:
            raise LambdaCloudProviderError(
                "Lambda Cloud launch succeeded but response did not include an external id"
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
        credentials = _lambda_cloud_credentials(request.credentials)
        response = await self._request(
            credentials,
            "GET",
            f"/instances/{_resource_id(request.external_id)}",
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
            credentials = _lambda_cloud_credentials(request.credentials)
            raw = await self._json_request(credentials, "GET", "/instances")
            resources = [_annotated_instance(item) for item in _instances_from_payload(raw)]

        updated = 0
        for resource in resources:
            resource_external_id = _instance_id(resource)
            if resource_external_id is None or not _should_mark_failed(resource):
                continue
            updated += await _mark_failed(
                request.context.pool,
                provider_id=request.provider_record.id,
                external_id=resource_external_id,
                reason=_failure_reason(resource),
                now=request.context.now,
            )

        return ReconcileResult(
            provider_id=request.provider_record.id,
            checked=len(resources),
            updated=updated,
            raw={"resources": resources},
        )

    async def teardown(self, request: TeardownRequest) -> TeardownResult:
        credentials = _lambda_cloud_credentials(request.credentials)
        external_id = await _external_id_for_lease(request.context.pool, request.lease_id)
        if external_id is None:
            raise LambdaCloudProviderError(
                "Lambda Cloud teardown requires a persisted external id "
                f"for lease {request.lease_id!r}"
            )

        response = await self._request(
            credentials,
            "POST",
            "/instance-operations/terminate",
            json_body={"instance_ids": [_resource_id(external_id)]},
        )
        if response.status_code == 404:
            raw: dict[str, Any] = {"success": True, "already_absent": True}
        else:
            _raise_for_status(response)
            raw = _raw_object(_response_payload(response))

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
        credentials: LambdaCloudCredentials,
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
            base_url=credentials.lambda_api_url,
            timeout=self._timeout_s,
            transport=self._transport,
        ) as client:
            return await client.request(method, path, headers=headers, content=content)

    async def _json_request(
        self,
        credentials: LambdaCloudCredentials,
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
        workload_type="vm_lease",
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


async def _mark_failed(
    pool: Any,
    *,
    provider_id: str,
    external_id: str,
    reason: str,
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
            reason,
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


def _launch_instance_body(
    provider_record: ProviderRecord,
    payload: Mapping[str, Any],
    *,
    request_id: str | None,
) -> dict[str, Any]:
    config = _config_mapping(provider_record)
    body: dict[str, Any] = {}
    body.update(_mapping_value(config.get("launch")))
    body.update(_mapping_value(payload.get("lambda_launch")))
    body.update(_mapping_value(payload.get("instance")))
    body.update(_mapping_value(payload.get("launch")))

    for key in _LAUNCH_FIELD_KEYS:
        if key in config and key not in body:
            body[key] = config[key]
        if key in payload:
            body[key] = payload[key]

    if "region_name" not in body and provider_record.region is not None:
        body["region_name"] = provider_record.region
    if "name" not in body:
        body["name"] = _default_name(provider_record.id, request_id)

    _require_non_empty_string(body, "region_name")
    _require_non_empty_string(body, "instance_type_name")
    body["ssh_key_names"] = _non_empty_string_list(body.get("ssh_key_names"), "ssh_key_names")
    if "quantity" in body:
        body["quantity"] = _positive_int(body["quantity"], "quantity")
    return body


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
    data = payload.get("data")
    if isinstance(data, list):
        return [dict(item) for item in data if isinstance(item, Mapping)]
    if isinstance(data, Mapping):
        instances = data.get("instances")
        if isinstance(instances, list):
            return [dict(item) for item in instances if isinstance(item, Mapping)]
        if isinstance(instances, Mapping):
            return [dict(instances)]
        if _looks_like_instance(data):
            return [dict(data)]
    if _looks_like_instance(payload):
        return [dict(payload)]
    return []


def _instance_from_payload(payload: object, external_id: str) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data")
    if isinstance(data, Mapping):
        instance = _instance_from_mapping(data, external_id)
        if instance is not None:
            return instance
    if isinstance(data, list):
        for item in data:
            if isinstance(item, Mapping) and _instance_id(item) == external_id:
                return dict(item)
        return None
    return _instance_from_mapping(payload, external_id)


def _instance_from_mapping(
    payload: Mapping[str, Any],
    external_id: str,
) -> dict[str, Any] | None:
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
    if status in _RUNNING_STATUSES:
        return ResourceStatus.RUNNING
    if status in _PROVISIONING_STATUSES:
        return ResourceStatus.PROVISIONING
    if status in _TERMINATED_STATUSES:
        return ResourceStatus.TERMINATED
    if status in _FAILED_STATUSES:
        return ResourceStatus.FAILED
    return ResourceStatus.UNKNOWN


def _should_mark_failed(instance: Mapping[str, Any]) -> bool:
    return _resource_status(instance) == ResourceStatus.FAILED


def _is_preempted(instance: Mapping[str, Any]) -> bool:
    return _status_text(instance) in _PREEMPTED_STATUSES


def _failure_reason(instance: Mapping[str, Any]) -> str:
    if _is_preempted(instance):
        return "lambda_cloud_preempted"
    return "lambda_cloud_failed"


def _status_text(instance: Mapping[str, Any]) -> str:
    for key in ("status", "state", "lifecycle_state"):
        value = _normalized_text(instance.get(key))
        if value:
            return value
    return ""


def _instance_id(instance: Mapping[str, Any]) -> str | None:
    for key in ("id", "instance_id"):
        value = _optional_string(instance.get(key))
        if value is not None:
            return value
    return None


def _created_instance_id(payload: Mapping[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, Mapping):
        instance_ids = data.get("instance_ids")
        if isinstance(instance_ids, Sequence) and not isinstance(
            instance_ids, (bytes, bytearray, str)
        ):
            for item in instance_ids:
                value = _optional_string(item)
                if value is not None:
                    return value
        instance = data.get("instance")
        if isinstance(instance, Mapping):
            value = _instance_id(instance)
            if value is not None:
                return value
    for key in ("instance_id", "id"):
        value = _optional_string(payload.get(key))
        if value is not None:
            return value
    return None


def _looks_like_instance(payload: Mapping[str, Any]) -> bool:
    return any(key in payload for key in ("id", "instance_id", "status", "state"))


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
        raise LambdaCloudProviderError(str(exc)) from exc


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


def _require_non_empty_string(mapping: Mapping[str, Any], key: str) -> str:
    value = _optional_string(mapping.get(key))
    if value is None:
        raise LambdaCloudProviderError(f"Lambda Cloud launch requires {key}")
    return value


def _non_empty_string_list(value: object, name: str) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        items = [_optional_string(item) for item in value]
        strings = [item for item in items if item is not None]
        if strings:
            return strings
    raise LambdaCloudProviderError(f"Lambda Cloud launch requires non-empty {name}")


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


def _default_name(provider_id: str, request_id: str | None) -> str:
    if request_id is not None and request_id.strip():
        suffix = request_id.strip()
    else:
        suffix = uuid.uuid4().hex[:12]
    return f"pitwall-{provider_id}-{suffix}"[:64]


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
        return "lambda_cloud_failed"
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
        raise LambdaCloudProviderError(
            "Lambda Cloud resource id must be non-empty and contain no slashes"
        )
    return stripped


def _lambda_cloud_credentials(credentials: BaseModel) -> LambdaCloudCredentials:
    if isinstance(credentials, LambdaCloudCredentials):
        return credentials
    return LambdaCloudCredentials.model_validate(credentials)


__all__ = [
    "LAMBDA_CLOUD_API_URL",
    "LambdaCloudCredentials",
    "LambdaCloudProvider",
    "LambdaCloudProviderError",
]
