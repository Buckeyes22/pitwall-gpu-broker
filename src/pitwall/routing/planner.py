"""Pure route planner for the Stage 1-4 routing contract."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from enum import Enum
from typing import Any, Protocol, cast

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.routing.constraints import filter_hard_constraints
from pitwall.routing.context import PlanningContext, freeze_provider_snapshot
from pitwall.routing.cooldown import is_in_cooldown
from pitwall.routing.scoring import explain_score
from pitwall.routing.types import (
    CapacityDecision,
    CapacityProbeKey,
    ConstraintResult,
    EliminationReason,
    Hints,
    ObservedMetrics,
    ProviderEliminated,
    RouteAttempt,
    RouteCandidate,
    RouteElimination,
    RoutePlan,
    RoutingRequest,
)
from pitwall.runpod_client.availability import AvailabilityCache

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_S = 1.0

_ProviderLike = Provider | Mapping[str, Any]
_ObservedByProvider = Mapping[str, ObservedMetrics | Mapping[str, object] | float | int]


class _AvailabilityCacheLike(Protocol):
    def is_available(
        self,
        datacenter: str,
        gpu_name: str,
        cloud_type: str,
        gpu_count: int,
    ) -> bool | None: ...


def plan_route(
    request: RoutingRequest,
    providers: Iterable[_ProviderLike] | None = None,
    *,
    capability: Capability | None = None,
    capability_id: str | None = None,
    hints: Hints | None = None,
    observed: ObservedMetrics | _ObservedByProvider | None = None,
    observed_metrics: ObservedMetrics | _ObservedByProvider | None = None,
    context: PlanningContext | None = None,
    now: dt.datetime | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
    availability_cache: _AvailabilityCacheLike | None = None,
    capacity_cache: _AvailabilityCacheLike | None = None,
) -> RoutePlan:
    """Return a deterministic route plan without performing network calls.

    The planner combines Stage 1 hard constraints, Stage 2 health/cooldown
    gating, Stage 3 scoring, Stage 4 cached capacity checks for pod leases,
    and explicit provider-config fallback chains.
    ``fallback_chain`` entries in the result are capped at ``max_attempts`` and
    include the selected provider as attempt 1.
    """

    _validate_plan_options(max_attempts=max_attempts, backoff_base_s=backoff_base_s)
    planning_context, capacity_reader, context_supplied = _resolve_planning_inputs(
        context=context,
        now=now,
        availability_cache=availability_cache,
        capacity_cache=capacity_cache,
    )
    active_hints = hints or request.hints or Hints()
    active_observed = observed if observed is not None else observed_metrics
    observed_at = planning_context.now
    active_providers = _providers_for_plan(
        providers,
        context=planning_context,
        freeze_explicit=context_supplied,
    )
    active_capability = capability if capability is not None else planning_context.capability

    stage1 = filter_hard_constraints(
        request,
        active_providers,
        capability=active_capability,
        capability_id=capability_id,
    )
    eliminated: list[RouteElimination] = [
        _stage1_elimination(result) for result in stage1.eliminated
    ]

    stage2_eligible: list[_ProviderLike] = []
    for provider in stage1.passed:
        provider_id = _provider_id(provider)
        reasons = _stage2_elimination_reasons(provider, now=observed_at)
        if reasons:
            eliminated.append(
                RouteElimination(
                    provider_id=provider_id,
                    stage=2,
                    reason=reasons[0],
                    reasons=tuple(reasons),
                )
            )
        else:
            stage2_eligible.append(provider)

    candidates = [
        _candidate_for_provider(
            provider,
            hints=active_hints,
            observed=_observed_for_provider(
                _provider_id(provider),
                active_observed,
            ),
        )
        for provider in stage2_eligible
    ]
    ranked_candidates = _rank_candidates(candidates)
    stage4_candidates, capacity_decisions, capacity_eliminated = _apply_stage4_capacity(
        request,
        ranked_candidates,
        cache=capacity_reader,
    )
    eliminated.extend(capacity_eliminated)
    attempts = _route_attempts(
        stage4_candidates,
        max_attempts=max_attempts,
        backoff_base_s=backoff_base_s,
    )

    return RoutePlan(
        request=request,
        attempts=attempts,
        ranked_candidates=ranked_candidates,
        eliminated=tuple(eliminated),
        capacity_decisions=capacity_decisions,
        max_attempts=max_attempts,
    )


def _validate_plan_options(*, max_attempts: int, backoff_base_s: float) -> None:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if not math.isfinite(backoff_base_s) or backoff_base_s < 0:
        raise ValueError("backoff_base_s must be a finite non-negative number")


def _resolve_planning_inputs(
    *,
    context: PlanningContext | None,
    now: dt.datetime | None,
    availability_cache: _AvailabilityCacheLike | None,
    capacity_cache: _AvailabilityCacheLike | None,
) -> tuple[PlanningContext, _AvailabilityCacheLike, bool]:
    explicit_cache = _resolve_explicit_capacity_cache(
        availability_cache=availability_cache,
        capacity_cache=capacity_cache,
    )
    if context is not None:
        if now is not None:
            raise ValueError("context and now are mutually exclusive")
        if explicit_cache is not None:
            raise ValueError("context and availability_cache/capacity_cache are mutually exclusive")
        return context, context.availability_snapshot, True

    if isinstance(explicit_cache, AvailabilityCache):
        live_context = PlanningContext.live(now=now, availability_cache=explicit_cache)
        return live_context, live_context.availability_snapshot, False

    live_context = PlanningContext.live(now=now)
    if explicit_cache is not None:
        return live_context, explicit_cache, False
    return live_context, live_context.availability_snapshot, False


def _resolve_explicit_capacity_cache(
    *,
    availability_cache: _AvailabilityCacheLike | None,
    capacity_cache: _AvailabilityCacheLike | None,
) -> _AvailabilityCacheLike | None:
    if (
        availability_cache is not None
        and capacity_cache is not None
        and availability_cache is not capacity_cache
    ):
        raise ValueError("availability_cache and capacity_cache must not differ")
    if availability_cache is not None:
        return availability_cache
    if capacity_cache is not None:
        return capacity_cache
    return None


def _providers_for_plan(
    providers: Iterable[_ProviderLike] | None,
    *,
    context: PlanningContext,
    freeze_explicit: bool,
) -> tuple[_ProviderLike, ...]:
    if providers is not None:
        if freeze_explicit:
            return cast(tuple[_ProviderLike, ...], freeze_provider_snapshot(providers))
        return tuple(providers)
    if context.providers:
        return cast(tuple[_ProviderLike, ...], context.providers)
    raise ValueError("providers must be supplied directly or through PlanningContext")


def _stage1_elimination(result: ConstraintResult) -> RouteElimination:
    reasons = tuple(_provider_eliminated_from_stage1(reason) for reason in result.reasons)
    provider_id = result.provider_id
    if not isinstance(provider_id, str) or not provider_id:
        raise ValueError("constraint result must include provider_id")
    if not reasons:
        raise ValueError("failed constraint result must include reasons")
    return RouteElimination(
        provider_id=provider_id,
        stage=1,
        reason=reasons[0],
        reasons=reasons,
    )


def _provider_eliminated_from_stage1(reason: EliminationReason) -> ProviderEliminated:
    return ProviderEliminated(reason.value)


def _stage2_elimination_reasons(
    provider: _ProviderLike,
    *,
    now: dt.datetime,
) -> list[ProviderEliminated]:
    reasons: list[ProviderEliminated] = []

    enabled = _field(provider, "enabled")
    if enabled is False:
        reasons.append(ProviderEliminated.DISABLED)

    if _health_status(provider) == "unhealthy":
        reasons.append(ProviderEliminated.HEALTH_UNHEALTHY)

    if is_in_cooldown(provider, now=now):
        reasons.append(ProviderEliminated.HEALTH_COOLDOWN)

    return reasons


def _candidate_for_provider(
    provider: _ProviderLike,
    *,
    hints: Hints,
    observed: ObservedMetrics,
) -> RouteCandidate:
    provider_id = _provider_id(provider)
    explanation = explain_score(provider, hints, observed)
    return RouteCandidate(
        provider_id=provider_id,
        provider=provider,
        rank=0,
        score=explanation.final_score,
        score_explanation=explanation,
        fallback_for=_fallback_for(provider),
        explicit_fallback_chain=_explicit_fallback_chain(provider),
    )


def _rank_candidates(
    candidates: Iterable[RouteCandidate],
) -> tuple[RouteCandidate, ...]:
    sorted_candidates = sorted(candidates, key=_candidate_sort_key)
    return tuple(
        replace(candidate, rank=index) for index, candidate in enumerate(sorted_candidates, start=1)
    )


def _candidate_sort_key(candidate: RouteCandidate) -> tuple[float, int, str, str]:
    return (
        -candidate.score,
        _priority(cast(_ProviderLike, candidate.provider)),
        _name(cast(_ProviderLike, candidate.provider)),
        candidate.provider_id,
    )


def _apply_stage4_capacity(
    request: RoutingRequest,
    ranked_candidates: tuple[RouteCandidate, ...],
    *,
    cache: _AvailabilityCacheLike,
) -> tuple[
    tuple[RouteCandidate, ...],
    tuple[CapacityDecision, ...],
    list[RouteElimination],
]:
    available_candidates: list[RouteCandidate] = []
    decisions: list[CapacityDecision] = []
    eliminated: list[RouteElimination] = []

    for candidate in ranked_candidates:
        if not _is_pod_lease_provider(cast(_ProviderLike, candidate.provider)):
            available_candidates.append(candidate)
            continue

        decision = _capacity_decision_for_candidate(request, candidate, cache=cache)
        decisions.append(decision)
        if decision.available is True:
            available_candidates.append(candidate)
            continue

        eliminated.append(
            RouteElimination(
                provider_id=candidate.provider_id,
                stage=4,
                reason=ProviderEliminated.CAPACITY_UNAVAILABLE,
            )
        )

    return tuple(available_candidates), tuple(decisions), eliminated


def _capacity_decision_for_candidate(
    request: RoutingRequest,
    candidate: RouteCandidate,
    *,
    cache: _AvailabilityCacheLike,
) -> CapacityDecision:
    keys = _capacity_keys_for_provider(request, cast(_ProviderLike, candidate.provider))
    if not keys:
        return CapacityDecision(
            provider_id=candidate.provider_id,
            available=None,
            reason="missing_capacity_key",
        )

    saw_unknown = False
    for key in keys:
        available = cache.is_available(
            key.datacenter,
            key.gpu_name,
            key.cloud_type,
            key.gpu_count,
        )
        if available is True:
            return CapacityDecision(
                provider_id=candidate.provider_id,
                available=True,
                reason="available",
                keys=keys,
                selected_key=key,
            )
        if available is None:
            saw_unknown = True

    return CapacityDecision(
        provider_id=candidate.provider_id,
        available=None if saw_unknown else False,
        reason="capacity_unknown" if saw_unknown else "capacity_unavailable",
        keys=keys,
    )


def _route_attempts(
    ranked_candidates: tuple[RouteCandidate, ...],
    *,
    max_attempts: int,
    backoff_base_s: float,
) -> tuple[RouteAttempt, ...]:
    if not ranked_candidates:
        return ()

    selected = _select_primary_candidate(ranked_candidates)
    fallback_candidates = _fallback_candidates(selected, ranked_candidates)
    chain = (selected, *fallback_candidates)[:max_attempts]

    return tuple(
        RouteAttempt(
            provider_id=candidate.provider_id,
            provider=candidate.provider,
            attempt=index,
            score=candidate.score,
            score_explanation=candidate.score_explanation,
            backoff_before_attempt_s=_backoff_before_attempt(index, backoff_base_s),
        )
        for index, candidate in enumerate(chain, start=1)
    )


def _select_primary_candidate(
    ranked_candidates: tuple[RouteCandidate, ...],
) -> RouteCandidate:
    explicit_fallback_ids = {
        provider_id
        for candidate in ranked_candidates
        for provider_id in candidate.explicit_fallback_chain
    }
    for candidate in ranked_candidates:
        if not candidate.fallback_for and candidate.provider_id not in explicit_fallback_ids:
            return candidate
    return ranked_candidates[0]


def _fallback_candidates(
    selected: RouteCandidate,
    ranked_candidates: tuple[RouteCandidate, ...],
) -> tuple[RouteCandidate, ...]:
    by_id = {candidate.provider_id: candidate for candidate in ranked_candidates}
    seen = {selected.provider_id}
    candidates: list[RouteCandidate] = []

    if selected.explicit_fallback_chain:
        for provider_id in selected.explicit_fallback_chain:
            candidate = by_id.get(provider_id)
            if candidate is None or candidate.provider_id in seen:
                continue
            seen.add(candidate.provider_id)
            candidates.append(candidate)
        return tuple(candidates)

    related = [
        candidate
        for candidate in ranked_candidates
        if selected.provider_id in candidate.fallback_for and candidate.provider_id not in seen
    ]
    return tuple(sorted(related, key=_fallback_sort_key))


def _fallback_sort_key(candidate: RouteCandidate) -> tuple[int, float, str, str]:
    return (
        _priority(cast(_ProviderLike, candidate.provider)),
        -candidate.score,
        _name(cast(_ProviderLike, candidate.provider)),
        candidate.provider_id,
    )


def _backoff_before_attempt(attempt: int, backoff_base_s: float) -> float:
    if attempt == 1:
        return 0.0
    result: float = backoff_base_s * (2 ** (attempt - 2))
    return result


def _observed_for_provider(
    provider_id: str,
    observed: ObservedMetrics | _ObservedByProvider | None,
) -> ObservedMetrics:
    if observed is None:
        return ObservedMetrics()
    if isinstance(observed, ObservedMetrics):
        return observed

    if "recent_error_rate" in observed:
        return _coerce_observed_metrics(observed)

    provider_observed = observed.get(provider_id)
    return _coerce_observed_metrics(provider_observed)


def _coerce_observed_metrics(
    value: ObservedMetrics | Mapping[str, object] | float | int | None,
) -> ObservedMetrics:
    if value is None:
        return ObservedMetrics()
    if isinstance(value, ObservedMetrics):
        return value
    if isinstance(value, Mapping):
        recent_error_rate = value.get("recent_error_rate", 0.0)
        return ObservedMetrics(recent_error_rate=float(cast(float | int | str, recent_error_rate)))
    if isinstance(value, bool):
        raise ValueError("observed recent_error_rate must be numeric")
    return ObservedMetrics(recent_error_rate=float(value))


def _provider_id(provider: _ProviderLike) -> str:
    provider_id = _string_field(provider, "id")
    if provider_id is None:
        raise ValueError("provider must include a non-empty id")
    return provider_id


def _is_pod_lease_provider(provider: _ProviderLike) -> bool:
    return _provider_type(provider) == ProviderType.POD_LEASE.value


def _provider_type(provider: _ProviderLike) -> str | None:
    return _string_field(provider, "provider_type")


def _health_status(provider: _ProviderLike) -> str:
    return (_string_field(provider, "health_status") or "unknown").lower()


def _priority(provider: _ProviderLike) -> int:
    value = _field(provider, "priority")
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("priority must be an integer")
    return int(cast(int | float | str | bool, value))


def _name(provider: _ProviderLike) -> str:
    return _string_field(provider, "name") or ""


def _fallback_for(provider: _ProviderLike) -> tuple[str, ...]:
    return _string_tuple(
        _first_present(
            _field(provider, "fallback_for"),
            _config_value(provider, "fallback_for"),
        )
    )


def _explicit_fallback_chain(provider: _ProviderLike) -> tuple[str, ...]:
    return _string_tuple(
        _first_present(
            _field(provider, "fallback_chain"),
            _config_value(provider, "fallback_chain"),
            _config_value(provider, "fallback_provider_ids"),
            _config_value(provider, "fallbacks"),
        )
    )


def _capacity_keys_for_provider(
    request: RoutingRequest,
    provider: _ProviderLike,
) -> tuple[CapacityProbeKey, ...]:
    datacenter = _capacity_datacenter(provider)
    cloud_type = _capacity_cloud_type(provider)
    gpu_count = _capacity_gpu_count(provider)
    gpu_names = _capacity_gpu_names(request, provider)
    if datacenter is None or cloud_type is None or gpu_count is None or not gpu_names:
        return ()

    return tuple(
        CapacityProbeKey(
            datacenter=datacenter,
            gpu_name=gpu_name,
            cloud_type=cloud_type,
            gpu_count=gpu_count,
        )
        for gpu_name in gpu_names
    )


def _capacity_datacenter(provider: _ProviderLike) -> str | None:
    data_center_ids = _first_present(
        _config_value(provider, "dataCenterIds"),
        _config_value(provider, "data_center_ids"),
    )
    if isinstance(data_center_ids, str):
        return _non_empty_string(data_center_ids)
    if _is_sequence(data_center_ids):
        for item in cast(Sequence[Any], data_center_ids):
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


def _capacity_cloud_type(provider: _ProviderLike) -> str:
    cloud_type = _first_config_or_field_string(
        provider,
        "cloud_type",
        "cloudType",
    )
    normalized = (cloud_type or "SECURE").upper()
    if normalized == "ALL" and _provider_volume_id(provider) is not None:
        return "SECURE"
    return normalized


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
        gpu_count = int(cast(int | float | str | bool, raw))
    except (TypeError, ValueError):
        return None
    if gpu_count < 1:
        return None
    return gpu_count


def _capacity_gpu_names(
    request: RoutingRequest,
    provider: _ProviderLike,
) -> tuple[str, ...]:
    if request.required_gpu_class:
        return (request.required_gpu_class,)

    values: list[str] = []
    for key in ("gpu_name", "gpu_type", "gpu_type_id", "gpu_class"):
        if value := _first_config_or_field_string(provider, key):
            values.append(value)

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
                values.append(raw)
        elif _is_sequence(raw):
            values.extend(
                str(item).strip()
                for item in cast(Sequence[Any], raw)
                if item is not None and str(item).strip()
            )

    return tuple(dict.fromkeys(values))


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
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str) and value:
        return value
    return None


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str))


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise ValueError("fallback provider ids must be strings or sequences of strings")

    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("fallback provider ids must be non-empty strings")
        if item not in result:
            result.append(item)
    return tuple(result)


build_route_plan = plan_route
create_route_plan = plan_route
route_providers = plan_route


__all__ = [
    "DEFAULT_BACKOFF_BASE_S",
    "DEFAULT_MAX_ATTEMPTS",
    "PlanningContext",
    "build_route_plan",
    "create_route_plan",
    "plan_route",
    "route_providers",
]
