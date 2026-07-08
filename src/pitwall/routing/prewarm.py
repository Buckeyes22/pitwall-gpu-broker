"""Demand-forecast prewarm recommendations for routing capacity.

This module is deliberately pure: it consumes recent request counts and provider
snapshots, then emits recommendations that an operator or reconciler can apply
elsewhere. It never provisions pods or mutates serverless endpoints.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Any, cast

from pitwall.core.enums import ProviderType
from pitwall.core.models import Provider
from pitwall.routing.cooldown import is_in_cooldown

_ProviderLike = Provider | Mapping[str, Any]
_SUPPORTED_PREWARM_TYPES = frozenset(
    {
        ProviderType.SERVERLESS_LB.value,
        ProviderType.SERVERLESS_QUEUE.value,
        ProviderType.POD_LEASE.value,
    }
)


class PrewarmTargetKind(StrEnum):
    """Kinds of warm capacity the planner can recommend."""

    ENDPOINT_WORKERS = "endpoint_workers"
    POD_LEASE = "pod_lease"


@dataclass(frozen=True, slots=True)
class DemandSample:
    """Recent request-count observation for one capability."""

    capability_id: str
    observed_at: dt.datetime
    request_count: int

    def __post_init__(self) -> None:
        if not self.capability_id:
            raise ValueError("capability_id must be non-empty")
        if self.request_count < 0:
            raise ValueError("request_count must be >= 0")
        object.__setattr__(
            self,
            "observed_at",
            _normalize_utc(self.observed_at, field_name="observed_at"),
        )


@dataclass(frozen=True, slots=True)
class PrewarmPolicy:
    """Tuning knobs for demand forecasting and warm-target sizing."""

    lookback: dt.timedelta = dt.timedelta(minutes=30)
    sample_window: dt.timedelta = dt.timedelta(minutes=5)
    forecast_window: dt.timedelta = dt.timedelta(minutes=5)
    forecast_horizon: dt.timedelta = dt.timedelta(minutes=5)
    headroom: float = 1.25
    default_requests_per_warm_unit: int = 20
    min_forecast_requests: int = 1
    max_targets_per_capability: int = 1
    default_lead_time: dt.timedelta = dt.timedelta(minutes=5)
    recommendation_ttl: dt.timedelta = dt.timedelta(minutes=15)

    def __post_init__(self) -> None:
        _validate_positive_duration(self.lookback, field_name="lookback")
        _validate_positive_duration(self.sample_window, field_name="sample_window")
        _validate_positive_duration(self.forecast_window, field_name="forecast_window")
        _validate_non_negative_duration(self.forecast_horizon, field_name="forecast_horizon")
        _validate_non_negative_duration(self.default_lead_time, field_name="default_lead_time")
        _validate_non_negative_duration(self.recommendation_ttl, field_name="recommendation_ttl")
        if self.lookback < self.sample_window:
            raise ValueError("lookback must be >= sample_window")
        if not math.isfinite(self.headroom) or self.headroom < 1.0:
            raise ValueError("headroom must be a finite number >= 1")
        if self.default_requests_per_warm_unit < 1:
            raise ValueError("default_requests_per_warm_unit must be >= 1")
        if self.min_forecast_requests < 0:
            raise ValueError("min_forecast_requests must be >= 0")
        if self.max_targets_per_capability < 1:
            raise ValueError("max_targets_per_capability must be >= 1")


@dataclass(frozen=True, slots=True)
class DemandForecast:
    """Forecasted request demand for one capability over a future window."""

    capability_id: str
    window_start: dt.datetime
    window_end: dt.datetime
    observed_counts: tuple[int, ...]
    projected_requests: int
    source_window_start: dt.datetime
    source_window_end: dt.datetime

    @property
    def projected_rps(self) -> float:
        duration_s = (self.window_end - self.window_start).total_seconds()
        if duration_s <= 0:
            return 0.0
        return self.projected_requests / duration_s

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "window_start": _isoformat_utc(self.window_start),
            "window_end": _isoformat_utc(self.window_end),
            "observed_counts": list(self.observed_counts),
            "projected_requests": self.projected_requests,
            "projected_rps": self.projected_rps,
            "source_window_start": _isoformat_utc(self.source_window_start),
            "source_window_end": _isoformat_utc(self.source_window_end),
        }


@dataclass(frozen=True, slots=True)
class PrewarmRecommendation:
    """One recommendation to raise warm capacity for a provider."""

    capability_id: str
    provider_id: str
    provider_type: str
    target_kind: PrewarmTargetKind
    target_count: int
    current_warm_count: int
    requests_per_warm_unit: int
    forecast_requests: int
    start_at: dt.datetime
    ready_by: dt.datetime
    expires_at: dt.datetime
    reason: str
    target: Mapping[str, object] = field(default_factory=dict)
    rank: int = 1

    def __post_init__(self) -> None:
        if not self.capability_id:
            raise ValueError("capability_id must be non-empty")
        if not self.provider_id:
            raise ValueError("provider_id must be non-empty")
        if self.target_count < 0:
            raise ValueError("target_count must be >= 0")
        if self.current_warm_count < 0:
            raise ValueError("current_warm_count must be >= 0")
        if self.requests_per_warm_unit < 1:
            raise ValueError("requests_per_warm_unit must be >= 1")
        if self.forecast_requests < 0:
            raise ValueError("forecast_requests must be >= 0")
        if self.rank < 1:
            raise ValueError("rank must be >= 1")
        object.__setattr__(self, "start_at", _normalize_utc(self.start_at, field_name="start_at"))
        object.__setattr__(self, "ready_by", _normalize_utc(self.ready_by, field_name="ready_by"))
        object.__setattr__(
            self, "expires_at", _normalize_utc(self.expires_at, field_name="expires_at")
        )
        object.__setattr__(self, "target", MappingProxyType(dict(self.target)))

    @property
    def delta(self) -> int:
        return max(0, self.target_count - self.current_warm_count)

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "provider_id": self.provider_id,
            "provider_type": self.provider_type,
            "target_kind": self.target_kind.value,
            "target_count": self.target_count,
            "current_warm_count": self.current_warm_count,
            "delta": self.delta,
            "requests_per_warm_unit": self.requests_per_warm_unit,
            "forecast_requests": self.forecast_requests,
            "start_at": _isoformat_utc(self.start_at),
            "ready_by": _isoformat_utc(self.ready_by),
            "expires_at": _isoformat_utc(self.expires_at),
            "reason": self.reason,
            "target": dict(self.target),
            "rank": self.rank,
        }


@dataclass(frozen=True, slots=True)
class PrewarmPlan:
    """Forecasts and warm-target recommendations for one planning instant."""

    now: dt.datetime
    forecasts: tuple[DemandForecast, ...] = field(default_factory=tuple)
    recommendations: tuple[PrewarmRecommendation, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "now", _normalize_utc(self.now, field_name="now"))
        object.__setattr__(self, "forecasts", tuple(self.forecasts))
        object.__setattr__(self, "recommendations", tuple(self.recommendations))

    def to_dict(self) -> dict[str, object]:
        return {
            "now": _isoformat_utc(self.now),
            "forecasts": [forecast.to_dict() for forecast in self.forecasts],
            "recommendations": [
                recommendation.to_dict() for recommendation in self.recommendations
            ],
        }


def forecast_demand(
    history: Iterable[DemandSample],
    *,
    now: dt.datetime,
    policy: PrewarmPolicy | None = None,
) -> tuple[DemandForecast, ...]:
    """Forecast the next near-term demand window per capability.

    The forecaster buckets recent counts into fixed windows ending at ``now``.
    The next-window estimate is the most recent bucket plus positive linear
    trend from the oldest bucket, multiplied by policy headroom.
    """

    active_policy = policy or PrewarmPolicy()
    observed_at = _normalize_utc(now, field_name="now")
    window_count = _sample_window_count(active_policy)
    source_window_start = observed_at - (active_policy.sample_window * window_count)
    buckets: dict[str, list[int]] = {}

    for sample in sorted(history, key=lambda item: (item.capability_id, item.observed_at)):
        _validate_sample(sample)
        if sample.observed_at < source_window_start or sample.observed_at >= observed_at:
            continue
        index = int((sample.observed_at - source_window_start) / active_policy.sample_window)
        if index < 0 or index >= window_count:
            continue
        counts = buckets.setdefault(sample.capability_id, [0 for _ in range(window_count)])
        counts[index] += sample.request_count

    forecast_start = observed_at + active_policy.forecast_horizon
    forecast_end = forecast_start + active_policy.forecast_window
    forecasts = [
        DemandForecast(
            capability_id=capability_id,
            window_start=forecast_start,
            window_end=forecast_end,
            observed_counts=tuple(counts),
            projected_requests=_project_requests(counts, headroom=active_policy.headroom),
            source_window_start=source_window_start,
            source_window_end=observed_at,
        )
        for capability_id, counts in sorted(buckets.items())
    ]
    return tuple(forecasts)


def plan_prewarm(
    history: Iterable[DemandSample],
    providers: Iterable[_ProviderLike],
    *,
    now: dt.datetime,
    policy: PrewarmPolicy | None = None,
) -> PrewarmPlan:
    """Return warm-target recommendations for recent demand and providers."""

    active_policy = policy or PrewarmPolicy()
    observed_at = _normalize_utc(now, field_name="now")
    forecasts = forecast_demand(history, now=observed_at, policy=active_policy)
    providers_by_capability = _eligible_providers_by_capability(providers, now=observed_at)
    recommendations: list[PrewarmRecommendation] = []

    for forecast in forecasts:
        if forecast.projected_requests < active_policy.min_forecast_requests:
            continue
        eligible = providers_by_capability.get(forecast.capability_id, ())
        for rank, provider in enumerate(
            eligible[: active_policy.max_targets_per_capability],
            start=1,
        ):
            recommendation = _recommendation_for_provider(
                forecast,
                provider,
                now=observed_at,
                policy=active_policy,
                rank=rank,
            )
            if recommendation is not None:
                recommendations.append(recommendation)

    return PrewarmPlan(
        now=observed_at,
        forecasts=forecasts,
        recommendations=tuple(recommendations),
    )


def _recommendation_for_provider(
    forecast: DemandForecast,
    provider: _ProviderLike,
    *,
    now: dt.datetime,
    policy: PrewarmPolicy,
    rank: int,
) -> PrewarmRecommendation | None:
    provider_type = _provider_type(provider)
    if provider_type is None:
        return None

    target_kind = _target_kind_for_provider_type(provider_type)
    if target_kind is None:
        return None

    requests_per_unit = _requests_per_warm_unit(
        provider,
        target_kind=target_kind,
        policy=policy,
    )
    target_count = math.ceil(forecast.projected_requests / requests_per_unit)
    current_warm_count = _current_warm_count(provider, target_kind=target_kind)
    if target_count <= current_warm_count:
        return None

    return PrewarmRecommendation(
        capability_id=forecast.capability_id,
        provider_id=_provider_id(provider),
        provider_type=provider_type,
        target_kind=target_kind,
        target_count=target_count,
        current_warm_count=current_warm_count,
        requests_per_warm_unit=requests_per_unit,
        forecast_requests=forecast.projected_requests,
        start_at=_recommendation_start_at(
            provider,
            now=now,
            ready_by=forecast.window_start,
            policy=policy,
        ),
        ready_by=forecast.window_start,
        expires_at=forecast.window_end + policy.recommendation_ttl,
        reason="forecast_exceeds_warm_capacity",
        target=_target_payload(provider, target_kind=target_kind),
        rank=rank,
    )


def _eligible_providers_by_capability(
    providers: Iterable[_ProviderLike],
    *,
    now: dt.datetime,
) -> dict[str, tuple[_ProviderLike, ...]]:
    grouped: dict[str, list[_ProviderLike]] = {}
    for provider in providers:
        if not _is_prewarm_eligible(provider, now=now):
            continue
        capability_id = _capability_id(provider)
        grouped.setdefault(capability_id, []).append(provider)

    return {
        capability_id: tuple(sorted(items, key=_provider_sort_key))
        for capability_id, items in sorted(grouped.items())
    }


def _is_prewarm_eligible(provider: _ProviderLike, *, now: dt.datetime) -> bool:
    if _field(provider, "enabled") is False:
        return False
    if (_string_field(provider, "health_status") or "unknown").lower() == "unhealthy":
        return False
    if is_in_cooldown(provider, now=now):
        return False
    provider_type = _provider_type(provider)
    return provider_type in _SUPPORTED_PREWARM_TYPES


def _target_kind_for_provider_type(provider_type: str) -> PrewarmTargetKind | None:
    if provider_type in {
        ProviderType.SERVERLESS_LB.value,
        ProviderType.SERVERLESS_QUEUE.value,
    }:
        return PrewarmTargetKind.ENDPOINT_WORKERS
    if provider_type == ProviderType.POD_LEASE.value:
        return PrewarmTargetKind.POD_LEASE
    return None


def _requests_per_warm_unit(
    provider: _ProviderLike,
    *,
    target_kind: PrewarmTargetKind,
    policy: PrewarmPolicy,
) -> int:
    prewarm_config = _prewarm_config(provider)
    keys = (
        ("requests_per_warm_pod", "requests_per_warm_pod_per_window")
        if target_kind == PrewarmTargetKind.POD_LEASE
        else ("requests_per_warm_worker", "requests_per_warm_worker_per_window")
    )
    for key in (*keys, "requests_per_warm_unit", "requests_per_warm_unit_per_window"):
        value = _first_present(prewarm_config.get(key), _config_value(provider, key))
        if value is not None:
            return _positive_int(value, field_name=key)
    return policy.default_requests_per_warm_unit


def _current_warm_count(
    provider: _ProviderLike,
    *,
    target_kind: PrewarmTargetKind,
) -> int:
    prewarm_config = _prewarm_config(provider)
    if target_kind == PrewarmTargetKind.POD_LEASE:
        return _non_negative_int(
            _first_present(
                prewarm_config.get("warm_pods"),
                _config_value(provider, "warm_pods"),
                _field(provider, "warm_pods"),
            ),
            default=0,
            field_name="warm_pods",
        )

    workers = _config_value(provider, "workers")
    worker_min: object = None
    if isinstance(workers, Mapping):
        worker_min = _first_present(
            workers.get("workers_min"),
            workers.get("workersMin"),
        )
    return _non_negative_int(
        _first_present(
            prewarm_config.get("workers_min"),
            prewarm_config.get("workersMin"),
            worker_min,
            _config_value(provider, "workers_min"),
            _config_value(provider, "workersMin"),
            _config_value(provider, "warm_workers"),
            _field(provider, "warm_workers"),
        ),
        default=0,
        field_name="workers_min",
    )


def _recommendation_start_at(
    provider: _ProviderLike,
    *,
    now: dt.datetime,
    ready_by: dt.datetime,
    policy: PrewarmPolicy,
) -> dt.datetime:
    provider_lead_time = _provider_lead_time(provider)
    lead_time = max(policy.default_lead_time, provider_lead_time)
    start_at = ready_by - lead_time
    if start_at < now:
        return now
    return start_at


def _provider_lead_time(provider: _ProviderLike) -> dt.timedelta:
    prewarm_config = _prewarm_config(provider)
    if lead_time_s := _first_present(
        prewarm_config.get("lead_time_s"),
        _config_value(provider, "prewarm_lead_time_s"),
    ):
        return dt.timedelta(seconds=_non_negative_float(lead_time_s, field_name="lead_time_s"))
    cold_start_p95_ms = _field(provider, "cold_start_p95_ms")
    if cold_start_p95_ms is None:
        return dt.timedelta(0)
    return dt.timedelta(
        milliseconds=_non_negative_float(cold_start_p95_ms, field_name="cold_start_p95_ms")
    )


def _target_payload(
    provider: _ProviderLike,
    *,
    target_kind: PrewarmTargetKind,
) -> Mapping[str, object]:
    if target_kind == PrewarmTargetKind.ENDPOINT_WORKERS:
        endpoint_id = _string_field(provider, "runpod_endpoint_id")
        if endpoint_id is None:
            return {}
        return {"runpod_endpoint_id": endpoint_id}

    payload: dict[str, object] = {}
    if template_id := _string_field(provider, "runpod_template_id"):
        payload["runpod_template_id"] = template_id
    if datacenter := _capacity_datacenter(provider):
        payload["datacenter"] = datacenter
    if gpu_name := _capacity_gpu_name(provider):
        payload["gpu_name"] = gpu_name
    if gpu_count := _capacity_gpu_count(provider):
        payload["gpu_count"] = gpu_count
    payload["cloud_type"] = _capacity_cloud_type(provider)
    return payload


def _project_requests(counts: Sequence[int], *, headroom: float) -> int:
    if not counts:
        return 0
    last = counts[-1]
    positive_trend = 0.0 if len(counts) == 1 else max(0.0, (last - counts[0]) / (len(counts) - 1))
    projected = max(0.0, float(last) + positive_trend)
    return math.ceil(projected * headroom)


def _sample_window_count(policy: PrewarmPolicy) -> int:
    return max(1, math.ceil(policy.lookback / policy.sample_window))


def _validate_sample(sample: DemandSample) -> None:
    if sample.request_count < 0:
        raise ValueError("request_count must be >= 0")
    _normalize_utc(sample.observed_at, field_name="observed_at")


def _provider_sort_key(provider: _ProviderLike) -> tuple[int, str, str]:
    return (_priority(provider), _name(provider), _provider_id(provider))


def _priority(provider: _ProviderLike) -> int:
    value = _field(provider, "priority")
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("priority must be an integer")
    return int(cast(int | float | str, value))


def _name(provider: _ProviderLike) -> str:
    return _string_field(provider, "name") or ""


def _provider_id(provider: _ProviderLike) -> str:
    provider_id = _string_field(provider, "id")
    if provider_id is None:
        raise ValueError("provider must include a non-empty id")
    return provider_id


def _capability_id(provider: _ProviderLike) -> str:
    capability_id = _string_field(provider, "capability_id")
    if capability_id is None:
        raise ValueError("provider must include a non-empty capability_id")
    return capability_id


def _provider_type(provider: _ProviderLike) -> str | None:
    return _string_field(provider, "provider_type")


def _prewarm_config(provider: _ProviderLike) -> Mapping[str, object]:
    value = _config_value(provider, "prewarm")
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return {}


def _capacity_datacenter(provider: _ProviderLike) -> str | None:
    data_center_ids = _first_present(
        _config_value(provider, "dataCenterIds"),
        _config_value(provider, "data_center_ids"),
    )
    if isinstance(data_center_ids, str):
        return _non_empty_string(data_center_ids)
    if _is_sequence(data_center_ids):
        for item in cast(Sequence[object], data_center_ids):
            if value := _non_empty_string(item):
                return value
    return _first_config_or_field_string(
        provider,
        "data_center_id",
        "dataCenterId",
        "datacenter",
        "data_center",
        "region",
    )


def _capacity_gpu_name(provider: _ProviderLike) -> str | None:
    for key in ("gpu_name", "gpu_type", "gpu_type_id", "gpu_class"):
        if value := _first_config_or_field_string(provider, key):
            return value

    for key in (
        "gpu_names",
        "gpu_types",
        "gpuTypeIds",
        "gpu_classes",
        "gpu_type_priority",
    ):
        raw = _config_value(provider, key)
        if isinstance(raw, str):
            if raw != "availability":
                return _non_empty_string(raw)
        elif _is_sequence(raw):
            for item in cast(Sequence[object], raw):
                if value := _non_empty_string(item):
                    return value
    return None


def _capacity_gpu_count(provider: _ProviderLike) -> int | None:
    raw = _first_present(
        _field(provider, "gpu_count"),
        _field(provider, "gpuCount"),
        _config_value(provider, "gpu_count"),
        _config_value(provider, "gpuCount"),
    )
    if raw is None:
        return 1
    if isinstance(raw, bool):
        return None
    try:
        gpu_count = int(cast(int | float | str, raw))
    except (TypeError, ValueError):
        return None
    if gpu_count < 1:
        return None
    return gpu_count


def _capacity_cloud_type(provider: _ProviderLike) -> str:
    cloud_type = _first_config_or_field_string(provider, "cloud_type", "cloudType")
    normalized = (cloud_type or "SECURE").upper()
    if normalized == "ALL" and _provider_volume_id(provider) is not None:
        return "SECURE"
    return normalized


def _provider_volume_id(provider: _ProviderLike) -> str | None:
    return _first_config_or_field_string(
        provider,
        "volume_id",
        "networkVolumeId",
        "network_volume_id",
        "required_volume",
    )


def _first_config_or_field_string(provider: _ProviderLike, *keys: str) -> str | None:
    for key in keys:
        value = _non_empty_string(_field(provider, key))
        if value is not None:
            return value
        value = _non_empty_string(_config_value(provider, key))
        if value is not None:
            return value
    return None


def _field(provider: _ProviderLike, key: str) -> object:
    if isinstance(provider, Mapping):
        return provider.get(key)
    return getattr(provider, key, None)


def _config(provider: _ProviderLike) -> Mapping[str, Any]:
    raw = _field(provider, "config")
    if isinstance(raw, Mapping):
        return raw
    return {}


def _config_value(provider: _ProviderLike, key: str) -> object:
    config = _config(provider)
    if key in config:
        return config[key]
    constraints = config.get("constraints")
    if isinstance(constraints, Mapping):
        return constraints.get(key)
    return None


def _first_present(*values: object) -> object:
    for value in values:
        if value is not None:
            return value
    return None


def _non_empty_string(value: object) -> str | None:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _string_field(provider: _ProviderLike, key: str) -> str | None:
    value = _field(provider, key)
    return _non_empty_string(value)


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str))


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    result = int(cast(int | float | str, value))
    if result < 1:
        raise ValueError(f"{field_name} must be >= 1")
    return result


def _non_negative_int(value: object, *, default: int, field_name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer")
    result = int(cast(int | float | str, value))
    if result < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return result


def _non_negative_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative number")
    result = float(cast(int | float | str, value))
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return result


def _validate_positive_duration(value: dt.timedelta, *, field_name: str) -> None:
    if value <= dt.timedelta(0):
        raise ValueError(f"{field_name} must be positive")


def _validate_non_negative_duration(value: dt.timedelta, *, field_name: str) -> None:
    if value < dt.timedelta(0):
        raise ValueError(f"{field_name} must be >= 0")


def _normalize_utc(value: dt.datetime, *, field_name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(dt.UTC)


def _isoformat_utc(value: dt.datetime) -> str:
    return _normalize_utc(value, field_name="datetime").isoformat().replace("+00:00", "Z")


__all__ = [
    "DemandForecast",
    "DemandSample",
    "PrewarmPlan",
    "PrewarmPolicy",
    "PrewarmRecommendation",
    "PrewarmTargetKind",
    "forecast_demand",
    "plan_prewarm",
]
