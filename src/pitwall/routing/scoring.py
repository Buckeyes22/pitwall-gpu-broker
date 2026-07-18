"""Stage 3 hint-based provider scoring."""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from pitwall.core.models import Provider
from pitwall.routing.types import Hints, ObservedMetrics, ScoreExplanation

_MISSING = object()
_ProviderLike = Provider | Mapping[str, Any]


def score_provider(
    provider: _ProviderLike,
    hints: Hints | None = None,
    observed: ObservedMetrics | None = None,
) -> float:
    """Return the Stage 3 routing score for a provider.

    Provider records in Pitwall persist some formula terms in ``config`` rather
    than as first-class model fields, so this
    function accepts both ``Provider`` objects and provider-shaped mappings.
    Missing optional numeric terms are neutral.
    """

    return explain_score(provider, hints, observed).final_score


def explain_score(
    provider: _ProviderLike,
    hints: Hints | None = None,
    observed: ObservedMetrics | None = None,
) -> ScoreExplanation:
    """Return the Stage 3 score plus each formula term."""

    active_hints = hints or Hints()
    active_observed = observed or ObservedMetrics()

    base_score = 100.0
    latency_penalty = 0.0
    warm_worker_bonus = 0.0
    if active_hints.latency_sensitive:
        latency_penalty = _cold_start_p50_ms(provider) / 100
        if _warm_workers(provider) >= 1:
            warm_worker_bonus = 20.0

    cost_penalty = 0.0
    if active_hints.cost_sensitive:
        cost_penalty = _cost_per_second_active(provider) * 10_000

    region_bonus = 0.0
    if (
        active_hints.region_preference is not None
        and active_hints.region_preference == _string_value(_field(provider, "region"))
    ):
        region_bonus = 15.0

    recent_error_penalty = _recent_error_rate(provider, active_observed) * 50
    priority_multiplier = _priority_multiplier(provider)
    score_before_multiplier = (
        base_score
        - latency_penalty
        + warm_worker_bonus
        - cost_penalty
        + region_bonus
        - recent_error_penalty
    )
    final_score = score_before_multiplier * priority_multiplier

    return ScoreExplanation(
        provider_id=_provider_id(provider),
        base_score=base_score,
        latency_penalty=latency_penalty,
        warm_worker_bonus=warm_worker_bonus,
        cost_penalty=cost_penalty,
        region_bonus=region_bonus,
        recent_error_penalty=recent_error_penalty,
        priority_multiplier=priority_multiplier,
        score_before_multiplier=score_before_multiplier,
        final_score=float(final_score),
    )


def _cold_start_p50_ms(provider: _ProviderLike) -> float:
    return _non_negative_number(
        _first_present(
            _field(provider, "cold_start_p50_ms"),
            _config_value(provider, "cold_start_p50_ms"),
        ),
        "cold_start_p50_ms",
        default=0.0,
    )


def _warm_workers(provider: _ProviderLike) -> float:
    return _non_negative_number(
        _first_present(
            _field(provider, "warm_workers"),
            _field(provider, "warmWorkers"),
            _field(provider, "workers"),
            _field(provider, "workers_min"),
            _field(provider, "workersMin"),
            _config_value(provider, "warm_workers"),
            _config_value(provider, "warmWorkers"),
            _config_value(provider, "workers"),
            _config_value(provider, "workers_min"),
            _config_value(provider, "workersMin"),
        ),
        "warm_workers",
        default=0.0,
    )


def _cost_per_second_active(provider: _ProviderLike) -> float:
    return _non_negative_number(
        _first_present(
            _field(provider, "cost_per_second_active"),
            _field(provider, "per_second_active"),
            _cost_value(provider, "cost_per_second_active"),
            _cost_value(provider, "per_second_active"),
            _config_value(provider, "cost_per_second_active"),
            _config_value(provider, "per_second_active"),
        ),
        "cost_per_second_active",
        default=0.0,
    )


def _recent_error_rate(provider: _ProviderLike, observed: ObservedMetrics) -> float:
    value = _first_present(
        _field(provider, "recent_error_rate"),
        _config_value(provider, "recent_error_rate"),
        observed.recent_error_rate,
    )
    rate = _non_negative_number(value, "recent_error_rate", default=0.0)
    if rate > 1:
        raise ValueError("recent_error_rate must be <= 1")
    return rate


def _priority_multiplier(provider: _ProviderLike) -> float:
    return _non_negative_number(
        _first_present(
            _field(provider, "priority_multiplier"),
            _field(provider, "priorityMultiplier"),
            _config_value(provider, "priority_multiplier"),
            _config_value(provider, "priorityMultiplier"),
        ),
        "priority_multiplier",
        default=1.0,
    )


def _provider_id(provider: _ProviderLike) -> str:
    return _string_value(_field(provider, "id")) or ""


def _first_present(*values: object) -> object:
    for value in values:
        if value is not _MISSING and value is not None:
            return value
    return _MISSING


def _field(provider: _ProviderLike, key: str) -> object:
    if isinstance(provider, Mapping):
        return provider.get(key, _MISSING)
    return getattr(provider, key, _MISSING)


def _config(provider: _ProviderLike) -> Mapping[str, Any]:
    raw = _field(provider, "config")
    if isinstance(raw, Mapping):
        return raw
    return {}


def _config_value(provider: _ProviderLike, key: str) -> object:
    config = _config(provider)
    if key in config:
        return config[key]
    return _MISSING


def _cost_value(provider: _ProviderLike, key: str) -> object:
    direct = _field(provider, "cost")
    if isinstance(direct, Mapping) and key in direct:
        return direct[key]

    config_cost = _config(provider).get("cost")
    if isinstance(config_cost, Mapping) and key in config_cost:
        return config_cost[key]

    return _MISSING


def _string_value(value: object) -> str | None:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str) and value:
        return value
    return None


def _non_negative_number(value: object, name: str, *, default: float) -> float:
    if value is _MISSING or value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")

    try:
        parsed = float(Decimal(str(value)))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc

    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


__all__ = ["ScoreExplanation", "explain_score", "score_provider"]
