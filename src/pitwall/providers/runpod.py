"""RunPod provider plugin adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, field_validator

from pitwall.api.leases import launch as lease_launch
from pitwall.api.leases import teardown as lease_teardown
from pitwall.core.models import Capability
from pitwall.core.models import Provider as ProviderRecord
from pitwall.cost.estimator import TaggedPricingModel, parse_pricing_model
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
from pitwall.runpod_client import pods as runpod_pods
from pitwall.runpod_client.graphql import RUNPOD_GRAPHQL_URL

SafeProviderUrl = Annotated[str, AfterValidator(lambda value: _safe_provider_url(value))]


class RunPodCredentials(BaseModel):
    """Credentials required for RunPod provider operations."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    api_key: SecretStr = Field(min_length=1)
    graphql_url: SafeProviderUrl = RUNPOD_GRAPHQL_URL
    rest_api_url: SafeProviderUrl | None = None

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("api_key must be non-empty")
        return value


class RunPodProvider:
    """Reference provider plugin backed by the existing RunPod services."""

    id = "runpod"
    name = "RunPod"
    credential_schema = RunPodCredentials

    def pricing_model(
        self,
        capability: Capability,
        provider_record: ProviderRecord,
    ) -> TaggedPricingModel:
        return parse_pricing_model(provider_record, cost_mode=capability.cost_mode)

    async def provision(self, request: ProvisionRequest) -> ProvisionResult:
        credentials = _runpod_credentials(request.credentials)
        raw = await lease_launch.run_launch(
            pool=request.context.pool,
            capability=request.capability,
            provider=request.provider_record,
            request_id=request.request_id,
            extra_env=dict(request.extra_env) if request.extra_env is not None else None,
            payload=dict(request.payload),
            budget_gate=request.budget_gate,
            idempotency_key=request.idempotency_key,
            dry_run=request.dry_run,
            **_launch_credential_kwargs(credentials),
        )
        return ProvisionResult(
            provider_id=_string_or_default(raw.get("provider_id"), request.provider_record.id),
            external_id=_optional_string(raw.get("pod_id")),
            lease_id=_optional_string(raw.get("lease_id")),
            raw=dict(raw),
        )

    async def status(self, request: StatusRequest) -> StatusResult:
        credentials = _runpod_credentials(request.credentials)
        pod = await runpod_pods._get_pod(
            request.external_id,
            strict_errors=True,
            **_rest_credential_kwargs(credentials),
        )
        if pod is None:
            return StatusResult(
                provider_id=request.provider_record.id,
                external_id=request.external_id,
                status=ResourceStatus.TERMINATED,
                raw={},
            )
        return StatusResult(
            provider_id=request.provider_record.id,
            external_id=request.external_id,
            status=_pod_status(pod),
            raw=dict(pod),
        )

    async def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        credentials = _runpod_credentials(request.credentials)
        if request.external_ids:
            resources: list[Mapping[str, Any]] = []
            for external_id in request.external_ids:
                status = await self.status(
                    StatusRequest(
                        context=request.context,
                        provider_record=request.provider_record,
                        credentials=request.credentials,
                        external_id=external_id,
                    )
                )
                resources.append(status.raw)
            return ReconcileResult(
                provider_id=request.provider_record.id,
                checked=len(resources),
                updated=0,
                raw={"resources": resources},
            )

        pods = await runpod_pods._get_pods(**_rest_credential_kwargs(credentials))
        return ReconcileResult(
            provider_id=request.provider_record.id,
            checked=len(pods),
            updated=0,
            raw={"resources": pods},
        )

    async def teardown(self, request: TeardownRequest) -> TeardownResult:
        credentials = _runpod_credentials(request.credentials)
        result = await lease_teardown.run_teardown(
            request.lease_id,
            pool=request.context.pool,
            redis_client=request.context.redis_client,
            reason=request.reason,
            now=request.context.now,
            terminal_state=request.terminal_state,
            **_rest_credential_kwargs(credentials),
        )
        lease = result.lease
        return TeardownResult(
            provider_id=lease.provider_id,
            lease_id=lease.id,
            external_id=lease.runpod_pod_id,
            raw={
                "event": result.event,
                "published_subscribers": result.published_subscribers,
                "state": _state_value(lease.state),
            },
        )


def _safe_provider_url(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("url must be non-empty")
    parsed = urlsplit(stripped)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not include user info")
    if parsed.query or parsed.fragment:
        raise ValueError("url must not include query strings or fragments")
    return stripped.rstrip("/")


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _runpod_credentials(value: object) -> RunPodCredentials:
    if isinstance(value, RunPodCredentials):
        return value
    return RunPodCredentials.model_validate(value)


def _api_key(credentials: RunPodCredentials) -> str:
    return credentials.api_key.get_secret_value()


def _launch_credential_kwargs(credentials: RunPodCredentials) -> dict[str, str]:
    kwargs = _rest_credential_kwargs(credentials)
    kwargs["graphql_url"] = credentials.graphql_url
    return kwargs


def _rest_credential_kwargs(credentials: RunPodCredentials) -> dict[str, str]:
    kwargs = {"api_key": _api_key(credentials)}
    if credentials.rest_api_url is not None:
        kwargs["rest_api_url"] = credentials.rest_api_url
    return kwargs


def _string_or_default(value: object, default: str) -> str:
    return _optional_string(value) or default


def _pod_status(pod: Mapping[str, Any]) -> ResourceStatus:
    status_text = _status_text(pod)
    if status_text in {"running", "ready"} or _has_runtime_signal(pod):
        return ResourceStatus.RUNNING
    if status_text in {"creating", "starting", "pending", "initializing"}:
        return ResourceStatus.PROVISIONING
    if status_text in {"failed", "error", "unhealthy"}:
        return ResourceStatus.FAILED
    if status_text in {"exited", "stopped", "terminated", "deleted"}:
        return ResourceStatus.TERMINATED
    return ResourceStatus.UNKNOWN


def _status_text(pod: Mapping[str, Any]) -> str:
    for key in ("desiredStatus", "desired_status", "status", "state"):
        value = pod.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _has_runtime_signal(pod: Mapping[str, Any]) -> bool:
    runtime = pod.get("runtime")
    return isinstance(runtime, Mapping) and bool(runtime)


def _state_value(state: object) -> str:
    value = getattr(state, "value", None)
    return value if isinstance(value, str) else str(state)


__all__ = [
    "RunPodCredentials",
    "RunPodProvider",
    "SafeProviderUrl",
]
