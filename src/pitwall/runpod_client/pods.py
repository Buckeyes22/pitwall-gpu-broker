"""RunPod Pod lifecycle client.

Pod create/list/find/delete uses RunPod's documented REST API. The Python SDK's
pod create helper builds GraphQL strings and only exposes ``dockerArgs``; REST
accepts structured JSON including ``dockerEntrypoint`` and ``dockerStartCmd``, which
is safer for command-heavy prewarm jobs and matches the current docs.

"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict, cast

import httpx
from pydantic import BaseModel, Field

from pitwall.runpod_client.registry import registry_auth_id_from_env
from pitwall.runpod_client.workloads import WorkloadConfig

log = logging.getLogger("pitwall.runpod_client.pods")

CAPACITY_ERROR_SUBSTRINGS_ENV = "PITWALL_RUNPOD_CAPACITY_ERROR_SUBSTRINGS"
VOLUME_ATTACH_TIMEOUT_ENV = "PITWALL_VOLUME_ATTACH_TIMEOUT_S"
DEFAULT_VOLUME_ATTACH_TIMEOUT_S = 300.0
DEFAULT_CAPACITY_ERROR_SUBSTRINGS = (
    "no longer any instances available",
    "resourcesunavailable",
    "not enough",
    "no instances",
    "insufficient",
    "unavailable",
    "does not have the resources",
)

CapacityErrorMatcher = Callable[[Exception], bool]
PROXY_PROBE_METHOD = "runpod_proxy"
SSH_LOCALHOST_PROBE_METHOD = "ssh_localhost"
POD_READINESS_PROBE_ORDER = (SSH_LOCALHOST_PROBE_METHOD, PROXY_PROBE_METHOD)
DEFAULT_POD_PROBE_TIMEOUT_S = 5.0


@dataclass
class PodProbeResult:
    """Structured result from a pod readiness probe.

    Attributes:
        healthy: True if the probe passed (2xx response).
        method: The probe method used (e.g., "ssh_localhost", "runpod_proxy").
        status_code: HTTP status code if available, None on timeout/connection error.
        error: Error type string if probe failed (e.g., "timeout", "524", "connection_error").
        latency_ms: Observed latency in milliseconds, None if request failed.
    """

    healthy: bool
    method: str
    status_code: int | None = None
    error: str | None = None
    latency_ms: float | None = None


class RunPodError(RuntimeError):
    """Any RunPod API failure that burst_api should surface as 5xx."""


class NoCapacityError(RunPodError):
    """All GPU types in the workload were exhausted (ResourcesUnavailable)."""


class ProviderFallbackRequested(NoCapacityError):
    """The current provider should be skipped after a paid pod pre-wait guard."""


class PodStartupTimeout(RunPodError):
    """RunPod accepted a pod, but it never reached a running runtime state."""


class PodVolumeAttachTimeout(PodStartupTimeout):
    """RunPod accepted a volume-attached pod, but the volume attach stayed hung."""

    def __init__(self, pod_id: str, attach_timeout_s: float) -> None:
        super().__init__(
            f"pod {pod_id} volume attach hang exceeded {attach_timeout_s:.0f}s (uptimeInSeconds=0)"
        )
        self.pod_id = pod_id
        self.attach_timeout_s = attach_timeout_s


class ProviderAttachHangRecoveryRequested(ProviderFallbackRequested):
    """The provider should cool down after a zero-uptime volume attach hang."""

    def __init__(self, message: str, *, pod_id: str, attach_timeout_s: float) -> None:
        super().__init__(message)
        self.pod_id = pod_id
        self.attach_timeout_s = attach_timeout_s


class PodStartupFailed(RunPodError):
    """RunPod accepted a pod, but the runtime reached a terminal failed state."""


class RunPodRestError(RunPodError):
    """RunPod REST API returned a non-2xx response."""

    def __init__(self, method: str, path: str, status_code: int, body: str) -> None:
        super().__init__(f"{method} {path} failed with HTTP {status_code}: {body}")
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body


class _RestAuthKwargs(TypedDict, total=False):
    api_key: str
    rest_api_url: str


class _SdkAuthKwargs(TypedDict, total=False):
    api_key: str


@dataclass
class _ReadinessSignals:
    runtime_seen_at: str | None = None
    port_mappings_seen_at: str | None = None
    probe_passed_at: str | None = None
    probe_method: str | None = None

    @classmethod
    def from_pod(cls, pod: dict[str, Any]) -> _ReadinessSignals:
        readiness = pod.get("readiness")
        if not isinstance(readiness, dict):
            return cls()
        probe_method = readiness.get("probe_method")
        return cls(
            runtime_seen_at=_non_empty_string_or_none(readiness.get("runtime_seen_at")),
            port_mappings_seen_at=_non_empty_string_or_none(readiness.get("port_mappings_seen_at")),
            probe_passed_at=_non_empty_string_or_none(readiness.get("probe_passed_at")),
            probe_method=str(probe_method) if probe_method else None,
        )

    @property
    def complete(self) -> bool:
        return (
            self.runtime_seen_at is not None
            and self.port_mappings_seen_at is not None
            and self.probe_passed_at is not None
        )

    def as_dict(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.runtime_seen_at is not None:
            values["runtime_seen_at"] = self.runtime_seen_at
        if self.port_mappings_seen_at is not None:
            values["port_mappings_seen_at"] = self.port_mappings_seen_at
        if self.probe_passed_at is not None:
            values["probe_passed_at"] = self.probe_passed_at
        if self.probe_method is not None:
            values["probe_method"] = self.probe_method
        return values


def _non_empty_string_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _require_api_key(api_key: str | None = None) -> str:
    key = api_key if api_key is not None else os.environ.get("RUNPOD_API_KEY")
    if not key:
        raise RunPodError("RUNPOD_API_KEY not set in process env")
    return key


def _sdk(api_key: str | None = None) -> Any:
    """Lazy import + api-key set. Keeps import-light at module load."""
    import runpod  # type: ignore[import-untyped]  # noqa: PLC0415  # reason: SDK import depends on runtime API-key setup

    runpod.api_key = _require_api_key(api_key)
    return runpod


def _rest_base_url(rest_api_url: str | None = None) -> str:
    return (
        rest_api_url or os.environ.get("RUNPOD_REST_API_URL", "https://rest.runpod.io/v1")
    ).rstrip("/")


def _rest_headers(api_key: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_require_api_key(api_key)}",
        "Content-Type": "application/json",
    }


def _rest_auth_kwargs(api_key: str | None, rest_api_url: str | None) -> _RestAuthKwargs:
    kwargs: _RestAuthKwargs = {}
    if api_key is not None:
        kwargs["api_key"] = api_key
    if rest_api_url is not None:
        kwargs["rest_api_url"] = rest_api_url
    return kwargs


def _sdk_auth_kwargs(api_key: str | None) -> _SdkAuthKwargs:
    return {"api_key": api_key} if api_key is not None else {}


def _has_explicit_rest_auth(api_key: str | None, rest_api_url: str | None) -> bool:
    return api_key is not None or rest_api_url is not None


def _get_pod_sync_for_auth(
    pod_id: str,
    *,
    api_key: str | None,
    rest_api_url: str | None,
) -> dict[str, Any] | None:
    if _has_explicit_rest_auth(api_key, rest_api_url):
        return _get_pod_sync(pod_id, **_rest_auth_kwargs(api_key, rest_api_url))
    return get_pod_sync(pod_id)


def _terminate_pod_sync_for_auth(
    pod_id: str,
    *,
    api_key: str | None,
    rest_api_url: str | None,
) -> None:
    if _has_explicit_rest_auth(api_key, rest_api_url):
        _terminate_pod_sync(pod_id, **_rest_auth_kwargs(api_key, rest_api_url))
        return
    terminate_pod_sync(pod_id)


def _rest_request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: float = 60.0,
    api_key: str | None = None,
    rest_api_url: str | None = None,
) -> Any:
    url = f"{_rest_base_url(rest_api_url)}/{path.lstrip('/')}"
    with httpx.Client(timeout=timeout_s) as client:
        response = client.request(
            method,
            url,
            headers=_rest_headers(api_key),
            json=json_body,
            params=params,
        )
    if response.status_code == 204:
        return {}
    if response.status_code >= 400:
        raise RunPodRestError(method, path, response.status_code, response.text)
    if not response.content:
        return {}
    return response.json()


def _ports_for_rest(ports: str | None) -> list[str] | None:
    if not ports:
        return None
    return [item.strip() for item in ports.split(",") if item.strip()]


def _cloud_types_for_rest(cloud_type: str, *, network_volume_id: str | None = None) -> list[str]:
    normalized = cloud_type.upper()
    if network_volume_id:
        if normalized == "COMMUNITY":
            raise RunPodError(
                "Network volumes require cloud_type='SECURE' or 'ALL', "
                "not 'COMMUNITY' (RunPod policy)."
            )
        return ["SECURE"]
    if normalized == "ALL":
        return ["COMMUNITY", "SECURE"]
    if normalized in {"COMMUNITY", "SECURE"}:
        return [normalized]
    raise RunPodError(f"unsupported RunPod REST cloud_type {cloud_type!r}")


def _container_registry_auth_id(image_ref: str | None = None) -> str | None:
    return registry_auth_id_from_env(image_ref)


def _normalize_pod(pod: dict[str, Any]) -> dict[str, Any]:
    """Smooth over SDK-vs-REST response shape differences used by callers."""

    if "imageName" not in pod and "image" in pod:
        pod["imageName"] = pod.get("image")
    gpu = pod.get("gpu")
    machine = pod.get("machine")
    if isinstance(gpu, dict):
        pod.setdefault("gpuTypeId", gpu.get("id"))
        if not isinstance(machine, dict):
            machine = {}
            pod["machine"] = machine
        machine.setdefault("gpuDisplayName", gpu.get("displayName") or gpu.get("id"))
    if isinstance(machine, dict):
        pod.setdefault("gpuTypeId", machine.get("gpuTypeId"))
    return pod


def _sdk_get_pod_sync(pod_id: str, *, api_key: str | None = None) -> dict[str, Any] | None:
    """Fetch pod details through the SDK/GraphQL path as a runtime fallback."""
    try:
        pod = _sdk(**_sdk_auth_kwargs(api_key)).get_pod(pod_id)
    except Exception as exc:  # noqa: BLE001  # reason: SDK exposes unstable exception types
        log.debug("sdk get_pod(%s) failed: %s", pod_id, exc)
        return None
    return _normalize_pod(pod) if isinstance(pod, dict) else None


def _configured_capacity_error_substrings() -> tuple[str, ...]:
    raw_substrings = os.environ.get(CAPACITY_ERROR_SUBSTRINGS_ENV)
    if raw_substrings is None:
        return DEFAULT_CAPACITY_ERROR_SUBSTRINGS
    return tuple(
        substring.strip()
        for substring in raw_substrings.replace("\n", ",").split(",")
        if substring.strip()
    )


def _coerce_timeout_s(value: object, *, source: str) -> float:
    if value is None or isinstance(value, bool):
        raise RunPodError(f"{source} must be a number of seconds")
    try:
        timeout_s = float(cast(float | int | str, value))
    except (TypeError, ValueError) as exc:
        raise RunPodError(f"{source} must be a number of seconds") from exc
    if not math.isfinite(timeout_s) or timeout_s < 0:
        raise RunPodError(f"{source} must be >= 0")
    return timeout_s


def _volume_attach_timeout_s(override_s: float | None = None) -> float:
    if override_s is not None:
        return _coerce_timeout_s(override_s, source="volume_attach_timeout_s")
    raw_timeout = os.environ.get(VOLUME_ATTACH_TIMEOUT_ENV)
    if raw_timeout is None or not raw_timeout.strip():
        return DEFAULT_VOLUME_ATTACH_TIMEOUT_S
    return _coerce_timeout_s(raw_timeout, source=VOLUME_ATTACH_TIMEOUT_ENV)


def _capacity_error_search_text(exc: Exception) -> str:
    if isinstance(exc, RunPodRestError):
        return f"{exc.body}\n{exc}"
    return str(exc)


def _substring_capacity_error_matcher(exc: Exception) -> bool:
    message = _capacity_error_search_text(exc).lower()
    return any(
        substring.lower() in message for substring in _configured_capacity_error_substrings()
    )


CAPACITY_ERROR_MATCHERS: list[CapacityErrorMatcher] = [_substring_capacity_error_matcher]


def _log_unmatched_capacity_error(exc: Exception) -> None:
    if isinstance(exc, RunPodRestError):
        log.warning(
            "unmatched RunPod error while checking capacity match: "
            "method=%s path=%s status=%s body=%s",
            exc.method,
            exc.path,
            exc.status_code,
            exc.body,
        )
        return
    log.warning("unmatched RunPod error while checking capacity match: %s", exc)


def _is_capacity_error(
    exc: Exception,
    matchers: Iterable[CapacityErrorMatcher] | None = None,
    *,
    log_unmatched: bool = True,
) -> bool:
    capacity_matchers = CAPACITY_ERROR_MATCHERS if matchers is None else matchers
    if any(matcher(exc) for matcher in capacity_matchers):
        return True
    if log_unmatched:
        _log_unmatched_capacity_error(exc)
    return False


def _pod_cost_per_hr(pod: dict[str, Any]) -> Decimal:
    raw_cost = pod.get("costPerHr")
    if raw_cost is None:
        raw_cost = pod.get("cost_per_hr")
    if raw_cost is None:
        return Decimal("0")

    pod_id = str(pod.get("id") or "<unknown>")
    return _money_decimal(raw_cost, field_name="costPerHr", pod_id=pod_id)


def _money_decimal(value: object, *, field_name: str, pod_id: str) -> Decimal:
    if isinstance(value, bool):
        raise RunPodError(f"pod {pod_id} returned invalid {field_name} {value!r}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RunPodError(f"pod {pod_id} returned invalid {field_name} {value!r}") from exc

    if not parsed.is_finite() or parsed < 0:
        raise RunPodError(f"pod {pod_id} returned invalid {field_name} {value!r}")
    return parsed


def _max_cost_per_hr_decimal(max_cost_per_hr: float) -> Decimal:
    try:
        parsed = Decimal(str(max_cost_per_hr))
    except (InvalidOperation, ValueError) as exc:
        raise RunPodError(f"invalid max_cost_per_hr {max_cost_per_hr!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise RunPodError(f"invalid max_cost_per_hr {max_cost_per_hr!r}")
    return parsed


def _pod_has_cost(pod: dict[str, Any]) -> bool:
    return pod.get("costPerHr") is not None or pod.get("cost_per_hr") is not None


def _gate_pod_cost_before_readiness(
    pod_id: str,
    pod: dict[str, Any],
    *,
    max_cost_per_hr: float | None,
    api_key: str | None = None,
    rest_api_url: str | None = None,
) -> ProviderFallbackRequested | None:
    if max_cost_per_hr is None:
        return None

    try:
        cost_per_hr = _pod_cost_per_hr(pod)
    except RunPodError as exc:
        log.warning("%s; deleting before readiness wait", exc)
        _terminate_pod_sync(pod_id, **_rest_auth_kwargs(api_key, rest_api_url))
        raise
    max_cost = _max_cost_per_hr_decimal(max_cost_per_hr)
    if not cost_per_hr or cost_per_hr <= max_cost:
        return None

    cap_error = ProviderFallbackRequested(
        f"pod {pod_id} cost ${cost_per_hr:.2f}/hr exceeds max ${max_cost:.2f}/hr"
    )
    log.warning("%s; deleting before readiness wait", cap_error)
    _terminate_pod_sync_for_auth(pod_id, api_key=api_key, rest_api_url=rest_api_url)
    return cap_error


def _coerce_non_negative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0 or not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = float(stripped)
        except ValueError:
            return None
        if not math.isfinite(parsed) or parsed < 0 or not parsed.is_integer():
            return None
        return int(parsed)
    return None


def _pod_gpu_count(pod: dict[str, Any]) -> int | None:
    for key in ("gpuCount", "gpu_count"):
        count = _coerce_non_negative_int(pod.get(key))
        if count is not None:
            return count

    machine = pod.get("machine")
    if isinstance(machine, dict):
        for key in ("gpuCount", "gpu_count", "gpu_count_allocated"):
            count = _coerce_non_negative_int(machine.get(key))
            if count is not None:
                return count

    for key in ("gpus", "gpuIds", "gpu_ids"):
        value = pod.get(key)
        if isinstance(value, list | tuple):
            return len(value)
    return None


def _pod_allocated_gpu_type_id(pod: dict[str, Any]) -> str | None:
    for key in ("gpuTypeId", "gpu_type_id"):
        value = _non_empty_string_or_none(pod.get(key))
        if value is not None:
            return value

    machine = pod.get("machine")
    if isinstance(machine, dict):
        for key in ("gpuTypeId", "gpu_type_id"):
            value = _non_empty_string_or_none(machine.get(key))
            if value is not None:
                return value

    gpu = pod.get("gpu")
    if isinstance(gpu, dict):
        for key in ("id", "gpuTypeId", "gpu_type_id"):
            value = _non_empty_string_or_none(gpu.get(key))
            if value is not None:
                return value
    return None


def _has_explicit_empty_gpu_type_id(pod: dict[str, Any]) -> bool:
    if _pod_allocated_gpu_type_id(pod) is not None:
        return False
    if "gpuTypeId" in pod or "gpu_type_id" in pod:
        return True
    machine = pod.get("machine")
    if isinstance(machine, dict) and ("gpuTypeId" in machine or "gpu_type_id" in machine):
        return True
    gpu = pod.get("gpu")
    return isinstance(gpu, dict) and any(key in gpu for key in ("id", "gpuTypeId", "gpu_type_id"))


def _pod_allocation_refresh_needed(pod: dict[str, Any]) -> bool:
    if _pod_gpu_count(pod) is not None or _pod_allocated_gpu_type_id(pod) is not None:
        return False
    return any(key in pod for key in ("gpu", "machine", "gpuCount", "gpu_count"))


def _gate_pod_allocation_before_readiness(
    pod_id: str,
    pod: dict[str, Any],
    *,
    requested_gpu_type_ids: Iterable[str],
) -> ProviderFallbackRequested | None:
    gpu_count = _pod_gpu_count(pod)
    if gpu_count == 0:
        return ProviderFallbackRequested(f"pod {pod_id} allocated zero GPUs")

    if gpu_count is None and _has_explicit_empty_gpu_type_id(pod):
        return ProviderFallbackRequested(f"pod {pod_id} allocated zero GPUs")

    allocated_gpu_type = _pod_allocated_gpu_type_id(pod)
    requested = set(requested_gpu_type_ids)
    if allocated_gpu_type is not None and requested and allocated_gpu_type not in requested:
        return ProviderFallbackRequested(
            f"pod {pod_id} allocated GPU {allocated_gpu_type!r} outside requested "
            f"set {sorted(requested)!r}"
        )
    return None


def _copy_initial_runtime_fields(
    *,
    initial: dict[str, Any],
    refreshed: dict[str, Any],
) -> dict[str, Any]:
    if initial.get("ports") and not refreshed.get("ports"):
        refreshed["ports"] = initial["ports"]
    if initial.get("networkVolumeId") and not refreshed.get("networkVolumeId"):
        refreshed["networkVolumeId"] = initial["networkVolumeId"]
    return refreshed


def _refresh_pod_for_pre_readiness_guards(
    pod_id: str,
    pod: dict[str, Any],
    *,
    max_cost_per_hr: float | None,
    api_key: str | None = None,
    rest_api_url: str | None = None,
) -> dict[str, Any]:
    needs_cost_refresh = max_cost_per_hr is not None and not _pod_has_cost(pod)
    if not needs_cost_refresh and not _pod_allocation_refresh_needed(pod):
        return pod

    refreshed = _get_pod_sync_for_auth(pod_id, api_key=api_key, rest_api_url=rest_api_url)
    if refreshed is None:
        return pod
    return _copy_initial_runtime_fields(initial=pod, refreshed=refreshed)


def _create_pod_with_fallback_sync(
    *,
    name: str,
    template_id: str | None,
    image_name: str,
    workload: WorkloadConfig,
    env: dict[str, str],
    cloud_type_override: str | None = None,
    network_volume_id: str | None = None,
    data_center_id: str | None = None,
    docker_entrypoint: list[str] | None = None,
    docker_start_cmd: list[str] | None = None,
    container_registry_auth_id: str | None = None,
    support_public_ip: bool = False,
    max_cost_per_hr: float | None = None,
    max_pod_attempts: int | None = None,
    timeout_per_attempt_s: float = 120.0,
    startup_timeout_s: float = 600.0,
    startup_poll_s: float = 15.0,
    volume_attach_timeout_s: float | None = None,
    pre_readiness_callback: Callable[[dict[str, Any]], None] | None = None,
    wait_for_readiness: bool = True,
    api_key: str | None = None,
    rest_api_url: str | None = None,
) -> dict[str, Any]:
    """Launch a pod via REST, trying GPU/cloud fallbacks until one succeeds."""

    last_err: Exception | None = None
    pod_attempts = 0
    gpu_type_attempts = (
        [workload.gpu_types]
        if workload.gpu_type_priority == "availability"
        else [[gpu_type_id] for gpu_type_id in workload.gpu_types]
    )
    cloud_types_to_try = _cloud_types_for_rest(
        cloud_type_override or workload.cloud_type,
        network_volume_id=network_volume_id,
    )
    for gpu_type_ids in gpu_type_attempts:
        for cloud_type in cloud_types_to_try:
            gpu_label = ", ".join(gpu_type_ids)
            log.info(
                "create_pod attempt: gpu_types=%s priority=%s cloud=%s workload=%s dc=%s vol=%s",
                gpu_label,
                workload.gpu_type_priority,
                cloud_type,
                workload.name,
                data_center_id or "any",
                network_volume_id or "none",
            )
            payload: dict[str, Any] = {
                "name": name,
                "imageName": image_name,
                "computeType": "GPU",
                "cloudType": cloud_type,
                "gpuCount": workload.gpu_count,
                "gpuTypeIds": gpu_type_ids,
                "gpuTypePriority": workload.gpu_type_priority,
                "containerDiskInGb": workload.container_disk_gb,
                "minVCPUPerGPU": workload.min_vcpu,
                "minRAMPerGPU": workload.min_memory_gb,
                "supportPublicIp": support_public_ip,
                "env": env,
                "volumeMountPath": "/workspace",
            }
            if workload.allowed_cuda_versions:
                payload["allowedCudaVersions"] = workload.allowed_cuda_versions
            registry_auth_id = container_registry_auth_id or _container_registry_auth_id(image_name)
            if registry_auth_id:
                payload["containerRegistryAuthId"] = registry_auth_id
            if template_id:
                payload["templateId"] = template_id
            if network_volume_id:
                payload["networkVolumeId"] = network_volume_id
            if data_center_id:
                payload["dataCenterIds"] = [data_center_id]
                payload["dataCenterPriority"] = workload.data_center_priority
            if ports := _ports_for_rest(workload.ports):
                payload["ports"] = ports
            if docker_entrypoint is not None:
                payload["dockerEntrypoint"] = docker_entrypoint
            if docker_start_cmd is not None:
                payload["dockerStartCmd"] = docker_start_cmd

            try:
                pod = _normalize_pod(
                    _rest_request(
                        "POST",
                        "pods",
                        json_body=payload,
                        timeout_s=timeout_per_attempt_s,
                        **_rest_auth_kwargs(api_key, rest_api_url),
                    )
                )
            except Exception as exc:  # noqa: BLE001  # reason: API/client errors vary
                last_err = exc
                if _is_capacity_error(exc):
                    log.info(
                        "no capacity for gpu_types=%s cloud=%s, trying next",
                        gpu_label,
                        cloud_type,
                    )
                    continue
                raise RunPodError(
                    f"RunPod REST create pod failed for {gpu_label}/{cloud_type}: {exc}"
                ) from exc

            if pod and pod.get("id"):
                if network_volume_id:
                    pod.setdefault("networkVolumeId", network_volume_id)
                pod_attempts += 1
                pod_id = str(pod["id"])
                guarded_pod = _refresh_pod_for_pre_readiness_guards(
                    pod_id,
                    pod,
                    max_cost_per_hr=max_cost_per_hr,
                    api_key=api_key,
                    rest_api_url=rest_api_url,
                )
                cap_error = _gate_pod_allocation_before_readiness(
                    pod_id,
                    guarded_pod,
                    requested_gpu_type_ids=gpu_type_ids,
                )
                if cap_error is not None:
                    log.warning("%s; deleting before readiness wait", cap_error)
                    _terminate_pod_sync_for_auth(
                        pod_id,
                        api_key=api_key,
                        rest_api_url=rest_api_url,
                    )
                else:
                    cap_error = _gate_pod_cost_before_readiness(
                        pod_id,
                        guarded_pod,
                        max_cost_per_hr=max_cost_per_hr,
                        api_key=api_key,
                        rest_api_url=rest_api_url,
                    )
                if cap_error is not None:
                    last_err = cap_error
                    if max_pod_attempts is not None and pod_attempts >= max_pod_attempts:
                        raise RunPodError(
                            f"pod attempt limit {max_pod_attempts} reached for "
                            f"workload {workload.name!r}"
                        ) from last_err
                    continue
                log.info(
                    "pod created: id=%s gpu_type=%s cloud=%s dc=%s",
                    pod_id,
                    gpu_label,
                    cloud_type,
                    (guarded_pod.get("machine") or {}).get("dataCenterId", "?"),
                )
                pod_termination_invoked_after_post_create_error = False
                try:
                    if pre_readiness_callback is not None:
                        pre_readiness_callback(guarded_pod)
                    if not wait_for_readiness:
                        log.info("wait_for_readiness=False, returning raw pod after creation")
                        return guarded_pod
                    try:
                        wait_kwargs: dict[str, Any] = {
                            "initial": guarded_pod,
                            "timeout_s": startup_timeout_s,
                            "poll_s": startup_poll_s,
                        }
                        if volume_attach_timeout_s is not None:
                            wait_kwargs["volume_attach_timeout_s"] = volume_attach_timeout_s
                        if _has_explicit_rest_auth(api_key, rest_api_url):
                            wait_kwargs.update(_rest_auth_kwargs(api_key, rest_api_url))
                            return _wait_for_pod_runtime_sync(pod_id, **wait_kwargs)
                        return wait_for_pod_runtime_sync(pod_id, **wait_kwargs)
                    except PodVolumeAttachTimeout as exc:
                        last_err = ProviderAttachHangRecoveryRequested(
                            str(exc),
                            pod_id=pod_id,
                            attach_timeout_s=exc.attach_timeout_s,
                        )
                        log.warning(
                            "pod %s hit volume attach hang: %s; deleting before provider fallback",
                            pod_id,
                            exc,
                        )
                        pod_termination_invoked_after_post_create_error = True
                        _terminate_pod_sync_for_auth(
                            pod_id,
                            api_key=api_key,
                            rest_api_url=rest_api_url,
                        )
                        raise last_err from exc
                    except PodStartupTimeout as exc:
                        last_err = exc
                        log.warning(
                            "pod %s did not reach readiness: %s; deleting before next GPU",
                            pod_id,
                            exc,
                        )
                        pod_termination_invoked_after_post_create_error = True
                        _terminate_pod_sync_for_auth(
                            pod_id,
                            api_key=api_key,
                            rest_api_url=rest_api_url,
                        )
                        if max_pod_attempts is not None and pod_attempts >= max_pod_attempts:
                            raise RunPodError(
                                f"pod attempt limit {max_pod_attempts} reached for "
                                f"workload {workload.name!r}"
                            ) from last_err
                        continue
                    except PodStartupFailed:
                        log.warning("pod %s failed during startup; deleting", pod_id)
                        pod_termination_invoked_after_post_create_error = True
                        _terminate_pod_sync_for_auth(
                            pod_id,
                            api_key=api_key,
                            rest_api_url=rest_api_url,
                        )
                        raise
                finally:
                    if (
                        sys.exc_info()[0] is not None
                        and not pod_termination_invoked_after_post_create_error
                    ):
                        log.warning(
                            "pod %s failed after creation before readiness completed; deleting",
                            pod_id,
                        )
                        _terminate_pod_sync_for_auth(
                            pod_id,
                            api_key=api_key,
                            rest_api_url=rest_api_url,
                        )
            log.warning("create pod returned empty pod for gpu_types=%s", gpu_label)

    if isinstance(last_err, ProviderFallbackRequested):
        raise ProviderFallbackRequested(
            f"provider fallback requested for workload {workload.name!r}: {last_err}"
        ) from last_err
    raise NoCapacityError(
        f"all GPU types exhausted for workload {workload.name!r}: tried {workload.gpu_types}"
    ) from last_err


def create_pod_with_fallback_sync(
    *,
    name: str,
    template_id: str | None,
    image_name: str,
    workload: WorkloadConfig,
    env: dict[str, str],
    cloud_type_override: str | None = None,
    network_volume_id: str | None = None,
    data_center_id: str | None = None,
    docker_entrypoint: list[str] | None = None,
    docker_start_cmd: list[str] | None = None,
    container_registry_auth_id: str | None = None,
    support_public_ip: bool = False,
    max_cost_per_hr: float | None = None,
    max_pod_attempts: int | None = None,
    timeout_per_attempt_s: float = 120.0,
    startup_timeout_s: float = 600.0,
    startup_poll_s: float = 15.0,
    volume_attach_timeout_s: float | None = None,
    pre_readiness_callback: Callable[[dict[str, Any]], None] | None = None,
    wait_for_readiness: bool = True,
) -> dict[str, Any]:
    """Launch a pod via REST, trying GPU/cloud fallbacks until one succeeds."""

    return _create_pod_with_fallback_sync(
        name=name,
        template_id=template_id,
        image_name=image_name,
        workload=workload,
        env=env,
        cloud_type_override=cloud_type_override,
        network_volume_id=network_volume_id,
        data_center_id=data_center_id,
        docker_entrypoint=docker_entrypoint,
        docker_start_cmd=docker_start_cmd,
        container_registry_auth_id=container_registry_auth_id,
        support_public_ip=support_public_ip,
        max_cost_per_hr=max_cost_per_hr,
        max_pod_attempts=max_pod_attempts,
        timeout_per_attempt_s=timeout_per_attempt_s,
        startup_timeout_s=startup_timeout_s,
        startup_poll_s=startup_poll_s,
        volume_attach_timeout_s=volume_attach_timeout_s,
        pre_readiness_callback=pre_readiness_callback,
        wait_for_readiness=wait_for_readiness,
    )


async def _create_pod_with_fallback(
    *,
    name: str,
    template_id: str | None,
    image_name: str,
    workload: WorkloadConfig,
    env: dict[str, str],
    cloud_type_override: str | None = None,
    network_volume_id: str | None = None,
    data_center_id: str | None = None,
    docker_entrypoint: list[str] | None = None,
    docker_start_cmd: list[str] | None = None,
    container_registry_auth_id: str | None = None,
    support_public_ip: bool = False,
    max_cost_per_hr: float | None = None,
    max_pod_attempts: int | None = None,
    timeout_per_attempt_s: float = 120.0,
    startup_timeout_s: float = 600.0,
    startup_poll_s: float = 15.0,
    volume_attach_timeout_s: float | None = None,
    pre_readiness_callback: Callable[[dict[str, Any]], None] | None = None,
    wait_for_readiness: bool = True,
    api_key: str | None = None,
    rest_api_url: str | None = None,
) -> dict[str, Any]:
    """Async wrapper around the documented REST pod create flow."""

    sync_create = (
        _create_pod_with_fallback_sync
        if _has_explicit_rest_auth(api_key, rest_api_url)
        else create_pod_with_fallback_sync
    )
    create_kwargs: dict[str, Any] = {
        "name": name,
        "template_id": template_id,
        "image_name": image_name,
        "workload": workload,
        "env": env,
        "cloud_type_override": cloud_type_override,
        "network_volume_id": network_volume_id,
        "data_center_id": data_center_id,
        "docker_entrypoint": docker_entrypoint,
        "docker_start_cmd": docker_start_cmd,
        "container_registry_auth_id": container_registry_auth_id,
        "support_public_ip": support_public_ip,
        "max_cost_per_hr": max_cost_per_hr,
        "max_pod_attempts": max_pod_attempts,
        "timeout_per_attempt_s": timeout_per_attempt_s,
        "startup_timeout_s": startup_timeout_s,
        "startup_poll_s": startup_poll_s,
        "volume_attach_timeout_s": volume_attach_timeout_s,
        "pre_readiness_callback": pre_readiness_callback,
        "wait_for_readiness": wait_for_readiness,
    }
    if _has_explicit_rest_auth(api_key, rest_api_url):
        create_kwargs.update(_rest_auth_kwargs(api_key, rest_api_url))
    return await asyncio.to_thread(sync_create, **create_kwargs)


async def create_pod_with_fallback(
    *,
    name: str,
    template_id: str | None,
    image_name: str,
    workload: WorkloadConfig,
    env: dict[str, str],
    cloud_type_override: str | None = None,
    network_volume_id: str | None = None,
    data_center_id: str | None = None,
    docker_entrypoint: list[str] | None = None,
    docker_start_cmd: list[str] | None = None,
    container_registry_auth_id: str | None = None,
    support_public_ip: bool = False,
    max_cost_per_hr: float | None = None,
    max_pod_attempts: int | None = None,
    timeout_per_attempt_s: float = 120.0,
    startup_timeout_s: float = 600.0,
    startup_poll_s: float = 15.0,
    volume_attach_timeout_s: float | None = None,
    pre_readiness_callback: Callable[[dict[str, Any]], None] | None = None,
    wait_for_readiness: bool = True,
) -> dict[str, Any]:
    """Async wrapper around the documented REST pod create flow."""

    return await _create_pod_with_fallback(
        name=name,
        template_id=template_id,
        image_name=image_name,
        workload=workload,
        env=env,
        cloud_type_override=cloud_type_override,
        network_volume_id=network_volume_id,
        data_center_id=data_center_id,
        docker_entrypoint=docker_entrypoint,
        docker_start_cmd=docker_start_cmd,
        container_registry_auth_id=container_registry_auth_id,
        support_public_ip=support_public_ip,
        max_cost_per_hr=max_cost_per_hr,
        max_pod_attempts=max_pod_attempts,
        timeout_per_attempt_s=timeout_per_attempt_s,
        startup_timeout_s=startup_timeout_s,
        startup_poll_s=startup_poll_s,
        volume_attach_timeout_s=volume_attach_timeout_s,
        pre_readiness_callback=pre_readiness_callback,
        wait_for_readiness=wait_for_readiness,
    )


def _pod_runtime_state(pod: dict[str, Any]) -> str:
    runtime = pod.get("runtime") or {}
    for key in ("podStatus", "containerStatus", "status"):
        value = runtime.get(key)
        if value:
            return str(value)
    desired = pod.get("desiredStatus")
    if desired:
        return str(desired)
    return "unknown"


def _pod_has_runtime_signal(pod: dict[str, Any]) -> bool:
    runtime = pod.get("runtime")
    return isinstance(runtime, dict) and bool(runtime)


def _pod_has_port_mappings_signal(pod: dict[str, Any]) -> bool:
    if _has_mapping_value(pod.get("portMappings")):
        return True

    runtime = pod.get("runtime")
    if not isinstance(runtime, dict):
        return False
    return _has_mapping_value(runtime.get("portMappings")) or _has_mapping_value(
        runtime.get("ports")
    )


def _has_mapping_value(value: object) -> bool:
    if isinstance(value, dict | list | tuple):
        return bool(value)
    if isinstance(value, str | bytes):
        return bool(value)
    return value is not None


def _pod_has_runtime(pod: dict[str, Any]) -> bool:
    return _pod_has_runtime_signal(pod) and _pod_has_port_mappings_signal(pod)


def _pod_has_network_volume(pod: dict[str, Any]) -> bool:
    for key in ("networkVolumeId", "network_volume_id", "volumeId", "volume_id"):
        value = pod.get(key)
        if isinstance(value, str):
            if value.strip():
                return True
        elif value:
            return True

    network_volume = pod.get("networkVolume") or pod.get("network_volume")
    if isinstance(network_volume, dict):
        for key in ("id", "networkVolumeId", "volumeId"):
            value = network_volume.get(key)
            if isinstance(value, str):
                if value.strip():
                    return True
            elif value:
                return True
        return bool(network_volume)
    if isinstance(network_volume, str):
        return bool(network_volume.strip())
    return bool(network_volume)


def _coerce_uptime_seconds(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _pod_uptime_seconds(pod: dict[str, Any]) -> float | None:
    runtime = pod.get("runtime")
    if isinstance(runtime, dict):
        for key in ("uptimeInSeconds", "uptimeSeconds", "uptime"):
            uptime_s = _coerce_uptime_seconds(runtime.get(key))
            if uptime_s is not None:
                return uptime_s
    for key in ("uptimeSeconds", "uptimeInSeconds", "uptime"):
        uptime_s = _coerce_uptime_seconds(pod.get(key))
        if uptime_s is not None:
            return uptime_s
    return None


def _pod_has_zero_uptime(pod: dict[str, Any]) -> bool:
    uptime_s = _pod_uptime_seconds(pod)
    return uptime_s is not None and uptime_s <= 0


def _pod_http_probe_ports(pod: dict[str, Any]) -> list[int]:
    ports: list[int] = []
    seen: set[int] = set()

    def add_port(value: object) -> None:
        try:
            port = int(str(value).split("/", 1)[0])
        except (TypeError, ValueError):
            return
        if port < 1 or port > 65_535 or port in seen:
            return
        seen.add(port)
        ports.append(port)

    for declared in pod.get("ports") or []:
        try:
            raw_port, protocol = str(declared).split("/", 1)
        except (TypeError, ValueError):
            continue
        if protocol.lower() == "http":
            add_port(raw_port)

    runtime = pod.get("runtime")
    if isinstance(runtime, dict):
        _append_runtime_http_ports(runtime.get("ports"), add_port)
        _append_port_mapping_ports(runtime.get("portMappings"), add_port)
    _append_port_mapping_ports(pod.get("portMappings"), add_port)

    return ports


def _append_runtime_http_ports(
    runtime_ports: object,
    add_port: Callable[[object], None],
) -> None:
    if not isinstance(runtime_ports, list | tuple):
        return
    for runtime_port in runtime_ports:
        if not isinstance(runtime_port, dict):
            add_port(runtime_port)
            continue
        protocol = (
            runtime_port.get("type")
            or runtime_port.get("protocol")
            or runtime_port.get("privatePortType")
        )
        if protocol is not None and str(protocol).lower() != "http":
            continue
        add_port(
            runtime_port.get("privatePort")
            or runtime_port.get("containerPort")
            or runtime_port.get("port")
        )


def _append_port_mapping_ports(
    port_mappings: object,
    add_port: Callable[[object], None],
) -> None:
    if isinstance(port_mappings, dict):
        for raw_port in port_mappings:
            add_port(raw_port)
        return
    if not isinstance(port_mappings, list | tuple):
        return
    for mapping in port_mappings:
        if isinstance(mapping, dict):
            add_port(
                mapping.get("privatePort") or mapping.get("containerPort") or mapping.get("port")
            )
        else:
            add_port(mapping)


def _pod_http_proxy_ready(pod: dict[str, Any]) -> bool:
    pod_id = pod.get("id")
    if not pod_id:
        return False
    for port in _pod_http_probe_ports(pod):
        base_url = f"https://{pod_id}-{port}.proxy.runpod.net"
        for path in ("/health", "/status.json", "/"):
            try:
                response = httpx.get(
                    f"{base_url}{path}",
                    headers={"Accept": "*/*", "User-Agent": "curl/8.5.0"},
                    follow_redirects=True,
                    timeout=5,
                )
            except Exception:  # noqa: BLE001  # reason: proxy can return many transient errors
                continue
            if 200 <= response.status_code < 300:
                pod.setdefault("proxyUrl", base_url)
                return True
    return False


def _pod_http_proxy_probe(pod: dict[str, Any]) -> PodProbeResult:
    """Probe pod HTTP proxy with structured result and bounded timeout.

    Distinguishes 524 (Cloudflare timeout) from other errors as specified
    in the RunPod integration spec §4 landmine L4.
    """
    pod_id = pod.get("id")
    if not pod_id:
        return PodProbeResult(healthy=False, method=PROXY_PROBE_METHOD, error="no_pod_id")

    for port in _pod_http_probe_ports(pod):
        base_url = f"https://{pod_id}-{port}.proxy.runpod.net"
        for path in ("/health", "/status.json", "/"):
            start = time.monotonic()
            try:
                response = httpx.get(
                    f"{base_url}{path}",
                    headers={"Accept": "*/*", "User-Agent": "curl/8.5.0"},
                    follow_redirects=True,
                    timeout=DEFAULT_POD_PROBE_TIMEOUT_S,
                )
                latency_ms = (time.monotonic() - start) * 1000
                if 200 <= response.status_code < 300:
                    pod.setdefault("proxyUrl", base_url)
                    return PodProbeResult(
                        healthy=True,
                        method=PROXY_PROBE_METHOD,
                        status_code=response.status_code,
                        latency_ms=latency_ms,
                    )
                if response.status_code == 524:
                    return PodProbeResult(
                        healthy=False,
                        method=PROXY_PROBE_METHOD,
                        status_code=524,
                        error="524",
                        latency_ms=latency_ms,
                    )
            except httpx.TimeoutException:
                latency_ms = (time.monotonic() - start) * 1000
                return PodProbeResult(
                    healthy=False,
                    method=PROXY_PROBE_METHOD,
                    error="timeout",
                    latency_ms=latency_ms,
                )
            except httpx.HTTPError:
                latency_ms = (time.monotonic() - start) * 1000
                return PodProbeResult(
                    healthy=False,
                    method=PROXY_PROBE_METHOD,
                    error="connection_error",
                    latency_ms=latency_ms,
                )

    return PodProbeResult(healthy=False, method=PROXY_PROBE_METHOD, error="no_port")


def _pod_ssh_localhost_probe(pod: dict[str, Any]) -> PodProbeResult:
    """Placeholder for SSH localhost pod probes.

    Pitwall's readiness order is SSH-first so operators can wire a bounded
    localhost probe without changing the readiness state machine. The default
    client does not open SSH sessions itself, so this probe reports unavailable
    and lets the RunPod proxy probe handle hermetic and default runtime checks.
    """

    if not pod.get("id"):
        return PodProbeResult(healthy=False, method=SSH_LOCALHOST_PROBE_METHOD, error="no_pod_id")
    return PodProbeResult(
        healthy=False,
        method=SSH_LOCALHOST_PROBE_METHOD,
        error="not_configured",
    )


def _pod_readiness_probe(pod: dict[str, Any]) -> PodProbeResult:
    """Run pod readiness probes in the configured order."""

    last_result: PodProbeResult | None = None
    for method in POD_READINESS_PROBE_ORDER:
        if method == SSH_LOCALHOST_PROBE_METHOD:
            result = _pod_ssh_localhost_probe(pod)
        elif method == PROXY_PROBE_METHOD:
            result = _pod_http_proxy_probe(pod)
        else:
            result = PodProbeResult(healthy=False, method=method, error="unknown_method")
        if result.healthy:
            return result
        last_result = result
    return last_result or PodProbeResult(
        healthy=False,
        method="none",
        error="no_probe_methods",
    )


def _observe_readiness_signals(
    pod: dict[str, Any],
    signals: _ReadinessSignals,
) -> None:
    if signals.runtime_seen_at is None and _pod_has_runtime_signal(pod):
        signals.runtime_seen_at = _utc_now_iso()
    if signals.port_mappings_seen_at is None and _pod_has_port_mappings_signal(pod):
        signals.port_mappings_seen_at = _utc_now_iso()
    if signals.probe_passed_at is None and signals.port_mappings_seen_at is not None:
        probe = _pod_readiness_probe(pod)
        if not probe.healthy:
            _attach_readiness_signals(pod, signals)
            return
        signals.probe_passed_at = _utc_now_iso()
        signals.probe_method = probe.method
    _attach_readiness_signals(pod, signals)


def _attach_readiness_signals(
    pod: dict[str, Any],
    signals: _ReadinessSignals,
) -> None:
    readiness = pod.get("readiness")
    if not isinstance(readiness, dict):
        readiness = {}
        pod["readiness"] = readiness
    readiness.update(signals.as_dict())


def _pod_failed_startup(pod: dict[str, Any]) -> bool:
    state = _pod_runtime_state(pod).lower()
    return state in {
        "cancelled",
        "canceled",
        "dead",
        "exited",
        "failed",
        "stopped",
        "terminated",
    }


def _wait_for_pod_runtime_sync(
    pod_id: str,
    *,
    initial: dict[str, Any] | None = None,
    timeout_s: float = 600.0,
    poll_s: float = 15.0,
    volume_attach_timeout_s: float | None = None,
    api_key: str | None = None,
    rest_api_url: str | None = None,
) -> dict[str, Any]:
    """Wait until RunPod reports all readiness signals for ``pod_id``.

    Pod creation can return a pod id before the container starts. That state
    still burns credits, so callers should not treat the pod as successfully
    launched until runtime, port mappings, and a successful probe are visible.
    """

    latest = initial or {"id": pod_id}
    signals = _ReadinessSignals.from_pod(latest)
    _observe_readiness_signals(latest, signals)
    if timeout_s <= 0:
        return latest

    started_at = time.monotonic()
    deadline = started_at + timeout_s
    attach_timeout_s: float | None = None
    attach_deadline: float | None = None
    while True:
        _observe_readiness_signals(latest, signals)
        if signals.complete:
            return latest
        if _pod_failed_startup(latest):
            raise PodStartupFailed(
                f"pod {pod_id} reached startup state {_pod_runtime_state(latest)!r}"
            )

        if attach_deadline is None and _pod_has_network_volume(latest):
            attach_timeout_s = _volume_attach_timeout_s(volume_attach_timeout_s)
            attach_deadline = started_at + attach_timeout_s

        now = time.monotonic()
        if (
            attach_deadline is not None
            and attach_timeout_s is not None
            and now >= attach_deadline
            and _pod_has_zero_uptime(latest)
        ):
            raise PodVolumeAttachTimeout(pod_id, attach_timeout_s)

        remaining = deadline - now
        if remaining <= 0:
            raise PodStartupTimeout(
                f"pod {pod_id} did not reach runtime within {timeout_s:.0f}s "
                f"(last state={_pod_runtime_state(latest)!r})"
            )

        time.sleep(min(max(poll_s, 0.1), remaining))
        refreshed = _get_pod_sync_for_auth(pod_id, api_key=api_key, rest_api_url=rest_api_url)
        if refreshed is None:
            raise PodStartupFailed(f"pod {pod_id} disappeared before runtime was ready")
        if latest.get("ports") and not refreshed.get("ports"):
            refreshed["ports"] = latest["ports"]
        _attach_readiness_signals(refreshed, signals)
        latest = refreshed


def wait_for_pod_runtime_sync(
    pod_id: str,
    *,
    initial: dict[str, Any] | None = None,
    timeout_s: float = 600.0,
    poll_s: float = 15.0,
    volume_attach_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Wait until RunPod reports all readiness signals for ``pod_id``."""

    return _wait_for_pod_runtime_sync(
        pod_id,
        initial=initial,
        timeout_s=timeout_s,
        poll_s=poll_s,
        volume_attach_timeout_s=volume_attach_timeout_s,
    )


async def _wait_for_pod_runtime(
    pod_id: str,
    *,
    initial: dict[str, Any] | None = None,
    timeout_s: float = 600.0,
    poll_s: float = 15.0,
    volume_attach_timeout_s: float | None = None,
    api_key: str | None = None,
    rest_api_url: str | None = None,
) -> dict[str, Any]:
    """Async wrapper for wait_for_pod_runtime_sync."""

    return await asyncio.to_thread(
        _wait_for_pod_runtime_sync,
        pod_id,
        initial=initial,
        timeout_s=timeout_s,
        poll_s=poll_s,
        volume_attach_timeout_s=volume_attach_timeout_s,
        api_key=api_key,
        rest_api_url=rest_api_url,
    )


async def wait_for_pod_runtime(
    pod_id: str,
    *,
    initial: dict[str, Any] | None = None,
    timeout_s: float = 600.0,
    poll_s: float = 15.0,
    volume_attach_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Async wrapper for wait_for_pod_runtime_sync."""

    return await _wait_for_pod_runtime(
        pod_id,
        initial=initial,
        timeout_s=timeout_s,
        poll_s=poll_s,
        volume_attach_timeout_s=volume_attach_timeout_s,
    )


def _get_pods_sync(
    *,
    api_key: str | None = None,
    rest_api_url: str | None = None,
) -> list[dict[str, Any]]:
    """Return all pods owned by the account's RunPod user."""
    result = _rest_request(
        "GET",
        "pods",
        params={"includeMachine": "true", "includeNetworkVolume": "true"},
        **_rest_auth_kwargs(api_key, rest_api_url),
    )
    if isinstance(result, list):
        return [_normalize_pod(p) for p in result if isinstance(p, dict)]
    if isinstance(result, dict):
        return [_normalize_pod(p) for p in result.values() if isinstance(p, dict)]
    log.warning("get_pods returned unexpected type %s", type(result).__name__)
    return []


def get_pods_sync() -> list[dict[str, Any]]:
    """Return all pods owned by the account's RunPod user."""

    return _get_pods_sync()


async def _get_pods(
    *,
    api_key: str | None = None,
    rest_api_url: str | None = None,
) -> list[dict[str, Any]]:
    """Async wrapper for get_pods_sync."""
    return await asyncio.to_thread(
        _get_pods_sync,
        api_key=api_key,
        rest_api_url=rest_api_url,
    )


async def get_pods() -> list[dict[str, Any]]:
    """Async wrapper for get_pods_sync."""

    return await _get_pods()


def _get_pod_sync(
    pod_id: str,
    *,
    api_key: str | None = None,
    rest_api_url: str | None = None,
    strict_errors: bool = False,
) -> dict[str, Any] | None:
    """Return a single pod's state, or None if not found."""
    try:
        result = _rest_request(
            "GET",
            f"pods/{pod_id}",
            params={"includeMachine": "true", "includeNetworkVolume": "true"},
            **_rest_auth_kwargs(api_key, rest_api_url),
        )
        pod = _normalize_pod(result) if isinstance(result, dict) else None
        if pod and (not _pod_has_runtime_signal(pod) or not _pod_has_port_mappings_signal(pod)):
            sdk_pod = _sdk_get_pod_sync(pod_id, api_key=api_key)
            if sdk_pod:
                for key in (
                    "runtime",
                    "portMappings",
                    "dockerId",
                    "uptimeSeconds",
                    "uptimeInSeconds",
                ):
                    if _pod_value_missing(pod.get(key)) and not _pod_value_missing(
                        sdk_pod.get(key)
                    ):
                        pod[key] = sdk_pod[key]
        return pod
    except RunPodRestError as exc:
        if exc.status_code == 404:
            return None
        if strict_errors:
            raise
        log.warning("get_pod(%s) failed: %s", pod_id, exc)
        raise
        return None
    except Exception as exc:  # noqa: BLE001  # reason: poll path degrades transient errors to None
        if strict_errors:
            raise RunPodError(f"get_pod({pod_id}) failed: {exc}") from exc
        log.warning("get_pod(%s) failed: %s", pod_id, exc)
        return None


def get_pod_sync(pod_id: str) -> dict[str, Any] | None:
    """Return a single pod's state, or None if not found."""

    return _get_pod_sync(pod_id)


async def _get_pod(
    pod_id: str,
    *,
    api_key: str | None = None,
    rest_api_url: str | None = None,
    strict_errors: bool = False,
) -> dict[str, Any] | None:
    """Async wrapper for get_pod_sync."""
    return await asyncio.to_thread(
        _get_pod_sync,
        pod_id,
        api_key=api_key,
        rest_api_url=rest_api_url,
        strict_errors=strict_errors,
    )


async def get_pod(pod_id: str) -> dict[str, Any] | None:
    """Async wrapper for get_pod_sync."""

    return await _get_pod(pod_id)


def _pod_value_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str | bytes | dict | list | tuple | set):
        return not bool(value)
    return False


def _terminate_pod_sync(
    pod_id: str,
    *,
    api_key: str | None = None,
    rest_api_url: str | None = None,
) -> None:
    """Irrevocably destroy a pod. Idempotent: silent on already-terminated."""
    try:
        _rest_request("DELETE", f"pods/{pod_id}", **_rest_auth_kwargs(api_key, rest_api_url))
    except RunPodRestError as exc:
        if exc.status_code == 404:
            log.info("pod %s already terminated or never existed", pod_id)
            return
        raise RunPodError(f"terminate_pod({pod_id}) failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001  # reason: normalize client errors to RunPodError
        raise RunPodError(f"terminate_pod({pod_id}) failed: {exc}") from exc


def terminate_pod_sync(pod_id: str) -> None:
    """Irrevocably destroy a pod. Idempotent: silent on already-terminated."""

    _terminate_pod_sync(pod_id)


async def _terminate_pod(
    pod_id: str,
    *,
    api_key: str | None = None,
    rest_api_url: str | None = None,
) -> None:
    """Async wrapper for terminate_pod_sync."""
    await asyncio.to_thread(
        _terminate_pod_sync,
        pod_id,
        api_key=api_key,
        rest_api_url=rest_api_url,
    )


async def terminate_pod(pod_id: str) -> None:
    """Async wrapper for terminate_pod_sync."""

    await _terminate_pod(pod_id)


async def terminate_all_with_tag(
    name_prefix: str = "pitwall-",
) -> int:
    """Kill-switch helper: terminate every pod whose name starts with ``name_prefix``.

    Returns the count terminated.
    """
    pods = await get_pods()
    killed = 0
    for p in pods:
        name = p.get("name") or ""
        pod_id = p.get("id")
        if not pod_id or not name.startswith(name_prefix):
            continue
        log.warning("kill switch: terminating pod %s (%s)", pod_id, name)
        await terminate_pod(pod_id)
        killed += 1
    return killed


async def get_pods_by_tag_prefix(
    name_prefix: str = "pitwall-",
) -> list[dict[str, Any]]:
    """Return pods whose name starts with ``name_prefix``.

    Returns a list of pod info dicts with ``id`` and ``name`` keys.
    """
    pods = await get_pods()
    matching: list[dict[str, Any]] = []
    for p in pods:
        name = p.get("name") or ""
        pod_id = p.get("id")
        if not pod_id or not name.startswith(name_prefix):
            continue
        matching.append({"id": pod_id, "name": name})
    return matching


async def terminate_all_with_tag_and_get_pods(
    name_prefix: str = "pitwall-",
) -> tuple[int, list[dict[str, Any]]]:
    """Kill-switch helper: terminate every pod whose name starts with ``name_prefix``.

    Returns a tuple of (count terminated, list of terminated pod info).
    Each pod info dict contains ``id`` and ``name`` keys.
    """
    pods = await get_pods()
    killed = 0
    terminated_pods: list[dict[str, Any]] = []
    for p in pods:
        name = p.get("name") or ""
        pod_id = p.get("id")
        if not pod_id or not name.startswith(name_prefix):
            continue
        log.warning("kill switch: terminating pod %s (%s)", pod_id, name)
        await terminate_pod(pod_id)
        killed += 1
        terminated_pods.append({"id": pod_id, "name": name})
    return killed, terminated_pods


class UpdatePodRequest(BaseModel):
    """Request body for PATCH /pods/{pod_id}.

    All fields are optional — RunPod applies only supplied fields.
    """

    model_config = {"extra": "forbid"}

    env: dict[str, str] | None = Field(
        default=None, description="Environment variables to set on the pod"
    )
    ports: list[str] | None = Field(
        default=None,
        description="Exposed ports in RunPod format (e.g. ['8000/http', '8080/tcp'])",
    )
    container_registry_auth_id: str | None = Field(
        default=None,
        alias="containerRegistryAuthId",
    )


class UpdatePodResponse(BaseModel):
    """Response body from PATCH /pods/{pod_id}."""

    model_config = {"extra": "allow"}

    id: str
    desired_status: str | None = Field(default=None, alias="desiredStatus")
    runtime: dict[str, Any] | None = None


def _start_pod_sync(pod_id: str) -> dict[str, Any]:
    """Start a stopped pod. Idempotent: RunPod returns the current pod state if already running."""
    try:
        return cast(dict[str, Any], _rest_request("POST", f"pods/{pod_id}/start"))
    except RunPodRestError as exc:
        raise RunPodError(f"start_pod({pod_id}) failed: {exc}") from exc


async def start_pod(pod_id: str) -> dict[str, Any]:
    """Async wrapper for start_pod_sync."""
    return await asyncio.to_thread(_start_pod_sync, pod_id)


def _stop_pod_sync(pod_id: str) -> dict[str, Any]:
    """Stop a running pod. Idempotent: RunPod returns the current pod state if already stopped."""
    try:
        return cast(dict[str, Any], _rest_request("POST", f"pods/{pod_id}/stop"))
    except RunPodRestError as exc:
        raise RunPodError(f"stop_pod({pod_id}) failed: {exc}") from exc


async def stop_pod(pod_id: str) -> dict[str, Any]:
    """Async wrapper for stop_pod_sync."""
    return await asyncio.to_thread(_stop_pod_sync, pod_id)


def _reset_pod_sync(pod_id: str) -> dict[str, Any]:
    """Reset a pod (stop + start in one operation). Idempotent per RunPod."""
    try:
        return cast(dict[str, Any], _rest_request("POST", f"pods/{pod_id}/reset"))
    except RunPodRestError as exc:
        raise RunPodError(f"reset_pod({pod_id}) failed: {exc}") from exc


async def reset_pod(pod_id: str) -> dict[str, Any]:
    """Async wrapper for reset_pod_sync."""
    return await asyncio.to_thread(_reset_pod_sync, pod_id)


def _restart_pod_sync(pod_id: str) -> dict[str, Any]:
    """Restart a pod: stop then start. Idempotent if the pod is already running."""
    _stop_pod_sync(pod_id)
    return _start_pod_sync(pod_id)


async def restart_pod(pod_id: str) -> dict[str, Any]:
    """Async wrapper for restart_pod_sync."""
    return await asyncio.to_thread(_restart_pod_sync, pod_id)


def _update_pod_sync(
    pod_id: str,
    *,
    env: dict[str, str] | None = None,
    ports: list[str] | None = None,
    container_registry_auth_id: str | None = None,
) -> dict[str, Any]:
    """Update mutable pod fields. Sends only supplied fields to RunPod."""
    payload: dict[str, Any] = {}
    if env is not None:
        payload["env"] = env
    if ports is not None:
        payload["ports"] = ports
    if container_registry_auth_id is not None:
        payload["containerRegistryAuthId"] = container_registry_auth_id
    if not payload:
        raise RunPodError(f"update_pod({pod_id}): at least one field must be supplied")
    try:
        return cast(dict[str, Any], _rest_request("PATCH", f"pods/{pod_id}", json_body=payload))
    except RunPodRestError as exc:
        raise RunPodError(f"update_pod({pod_id}) failed: {exc}") from exc


async def update_pod(
    pod_id: str,
    *,
    env: dict[str, str] | None = None,
    ports: list[str] | None = None,
    container_registry_auth_id: str | None = None,
) -> dict[str, Any]:
    """Async wrapper for update_pod_sync."""
    return await asyncio.to_thread(
        _update_pod_sync,
        pod_id,
        env=env,
        ports=ports,
        container_registry_auth_id=container_registry_auth_id,
    )


__all__ = [
    "RunPodError",
    "NoCapacityError",
    "ProviderAttachHangRecoveryRequested",
    "PodStartupFailed",
    "PodStartupTimeout",
    "PodVolumeAttachTimeout",
    "RunPodRestError",
    "UpdatePodRequest",
    "UpdatePodResponse",
    "create_pod_with_fallback",
    "create_pod_with_fallback_sync",
    "get_pods",
    "get_pods_sync",
    "get_pod",
    "get_pod_sync",
    "terminate_all_with_tag",
    "wait_for_pod_runtime",
    "wait_for_pod_runtime_sync",
    "terminate_pod",
    "terminate_pod_sync",
    "start_pod",
    "stop_pod",
    "reset_pod",
    "restart_pod",
    "update_pod",
]
