"""Cost×latency×quality scorecards for Pitwall observability.

Aggregates windows of observations into normalized per-entity scorecards
for routing and governance decisions. Pure analytics — no I/O.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

_Aggregator = Literal["mean", "median", "p95", "sum"]
_CompositeMethod = Literal["arithmetic", "geometric"]
_Direction = Literal["lower_is_better", "higher_is_better"]


@dataclass(frozen=True, slots=True)
class ScorecardObservation:
    """One raw observation used to build a scorecard."""

    provider_id: str
    capability_id: str
    cost_usd: Decimal
    latency_ms: float
    quality: float
    observed_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class EntityScorecard:
    """Normalized cost×latency×quality scorecard for one (provider, capability)."""

    provider_id: str
    capability_id: str
    cost_usd: Decimal
    latency_ms: float
    quality: float
    cost_normalized: float
    latency_normalized: float
    quality_normalized: float
    composite_score: float
    rank: int
    observation_count: int
    window_start: dt.datetime | None
    window_end: dt.datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "capability_id": self.capability_id,
            "cost_usd": float(self.cost_usd),
            "latency_ms": self.latency_ms,
            "quality": self.quality,
            "cost_normalized": self.cost_normalized,
            "latency_normalized": self.latency_normalized,
            "quality_normalized": self.quality_normalized,
            "composite_score": self.composite_score,
            "rank": self.rank,
            "observation_count": self.observation_count,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
        }


class ScorecardBuilder:
    """Deterministic scorecard builder from a window of observations.

    Aggregates observations by *(provider_id, capability_id)*, normalises each
    dimension against the peer group, and ranks by composite score.
    """

    _VALID_AGGREGATORS: set[_Aggregator] = {"mean", "median", "p95", "sum"}

    def __init__(
        self,
        *,
        cost_weight: float = 1.0,
        latency_weight: float = 1.0,
        quality_weight: float = 1.0,
        cost_aggregator: _Aggregator = "mean",
        latency_aggregator: _Aggregator = "mean",
        quality_aggregator: _Aggregator = "mean",
        composite_method: _CompositeMethod = "arithmetic",
    ) -> None:
        if cost_weight < 0 or latency_weight < 0 or quality_weight < 0:
            raise ValueError("weights must be non-negative")
        if composite_method not in ("arithmetic", "geometric"):
            raise ValueError("composite_method must be 'arithmetic' or 'geometric'")
        for agg, name in (
            (cost_aggregator, "cost_aggregator"),
            (latency_aggregator, "latency_aggregator"),
            (quality_aggregator, "quality_aggregator"),
        ):
            if agg not in self._VALID_AGGREGATORS:
                raise ValueError(f"{name} must be one of {self._VALID_AGGREGATORS}")

        self._cost_weight = float(cost_weight)
        self._latency_weight = float(latency_weight)
        self._quality_weight = float(quality_weight)
        self._cost_aggregator = cost_aggregator
        self._latency_aggregator = latency_aggregator
        self._quality_aggregator = quality_aggregator
        self._composite_method = composite_method

    def build(
        self,
        observations: Sequence[ScorecardObservation],
        *,
        now: dt.datetime | None = None,
    ) -> tuple[EntityScorecard, ...]:
        """Return scorecards for *observations*, ordered by composite score descending."""
        if not observations:
            return ()

        _ = now  # reserved for future time-bounded windows

        grouped = _group_observations(observations)
        aggregates = [
            _aggregate_group(provider_id, capability_id, group, self)
            for (provider_id, capability_id), group in grouped.items()
        ]

        cost_values = [float(a.cost_usd) for a in aggregates]
        latency_values = [a.latency_ms for a in aggregates]
        quality_values = [a.quality for a in aggregates]

        cost_norms = _normalize(
            cost_values, direction="lower_is_better", method=self._composite_method
        )
        latency_norms = _normalize(
            latency_values, direction="lower_is_better", method=self._composite_method
        )
        quality_norms = _normalize(
            quality_values, direction="higher_is_better", method=self._composite_method
        )

        scored: list[tuple[float, EntityScorecard]] = []
        for agg, c_norm, l_norm, q_norm in zip(
            aggregates, cost_norms, latency_norms, quality_norms, strict=True
        ):
            composite = _composite_score(
                c_norm,
                l_norm,
                q_norm,
                self._cost_weight,
                self._latency_weight,
                self._quality_weight,
                method=self._composite_method,
            )
            scored.append(
                (
                    composite,
                    EntityScorecard(
                        provider_id=agg.provider_id,
                        capability_id=agg.capability_id,
                        cost_usd=agg.cost_usd,
                        latency_ms=agg.latency_ms,
                        quality=agg.quality,
                        cost_normalized=c_norm,
                        latency_normalized=l_norm,
                        quality_normalized=q_norm,
                        composite_score=composite,
                        rank=0,
                        observation_count=agg.observation_count,
                        window_start=agg.window_start,
                        window_end=agg.window_end,
                    ),
                )
            )

        scored.sort(key=lambda x: (-x[0], x[1].provider_id, x[1].capability_id))
        result: list[EntityScorecard] = []
        for rank, (_, card) in enumerate(scored, start=1):
            result.append(
                EntityScorecard(
                    provider_id=card.provider_id,
                    capability_id=card.capability_id,
                    cost_usd=card.cost_usd,
                    latency_ms=card.latency_ms,
                    quality=card.quality,
                    cost_normalized=card.cost_normalized,
                    latency_normalized=card.latency_normalized,
                    quality_normalized=card.quality_normalized,
                    composite_score=card.composite_score,
                    rank=rank,
                    observation_count=card.observation_count,
                    window_start=card.window_start,
                    window_end=card.window_end,
                )
            )

        return tuple(result)


def observations_from_workloads(
    workloads: Sequence[Any],
    *,
    quality_extractor: Callable[[Any], float] | None = None,
) -> list[ScorecardObservation]:
    """Convert Workload (or workload-shaped) objects into scorecard observations.

    Default quality is ``1.0`` when ``error`` is absent/None, else ``0.0``.
    """
    observations: list[ScorecardObservation] = []
    for w in workloads:
        cost = getattr(w, "cost_actual_usd", None)
        if cost is None:
            cost = Decimal("0")
        elif isinstance(cost, (int, float, str)):
            cost = Decimal(str(cost))
        elif not isinstance(cost, Decimal):
            cost = Decimal("0")

        latency = getattr(w, "execution_ms", None)
        if latency is None:
            latency = 0.0
        else:
            try:
                latency = float(latency)
            except (TypeError, ValueError):
                latency = 0.0
        if not math.isfinite(latency) or latency < 0:
            latency = 0.0

        if quality_extractor is not None:
            quality = quality_extractor(w)
        else:
            error = getattr(w, "error", None)
            quality = 0.0 if error is not None else 1.0

        try:
            quality = float(quality)
        except (TypeError, ValueError):
            quality = 0.0
        if not math.isfinite(quality):
            quality = 0.0
        quality = max(0.0, min(1.0, quality))

        observed_at = getattr(w, "completed_at", None)
        if observed_at is not None and not isinstance(observed_at, dt.datetime):
            observed_at = None

        provider_id = getattr(w, "provider_id", None)
        capability_id = getattr(w, "capability_id", None)
        if not provider_id or not capability_id:
            continue

        observations.append(
            ScorecardObservation(
                provider_id=str(provider_id),
                capability_id=str(capability_id),
                cost_usd=cost,
                latency_ms=latency,
                quality=quality,
                observed_at=observed_at,
            )
        )
    return observations


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Aggregate:
    provider_id: str
    capability_id: str
    cost_usd: Decimal
    latency_ms: float
    quality: float
    observation_count: int
    window_start: dt.datetime | None
    window_end: dt.datetime | None


def _group_observations(
    observations: Sequence[ScorecardObservation],
) -> dict[tuple[str, str], list[ScorecardObservation]]:
    grouped: dict[tuple[str, str], list[ScorecardObservation]] = {}
    for obs in observations:
        key = (obs.provider_id, obs.capability_id)
        grouped.setdefault(key, []).append(obs)
    return grouped


def _aggregate_group(
    provider_id: str,
    capability_id: str,
    group: list[ScorecardObservation],
    builder: ScorecardBuilder,
) -> _Aggregate:
    costs = [obs.cost_usd for obs in group]
    latencies = [obs.latency_ms for obs in group]
    qualities = [obs.quality for obs in group]

    cost_agg = _apply_aggregator(costs, builder._cost_aggregator)
    latency_agg = _apply_aggregator(latencies, builder._latency_aggregator)
    quality_agg = _apply_aggregator(qualities, builder._quality_aggregator)

    timestamps = [obs.observed_at for obs in group if obs.observed_at is not None]
    window_start = min(timestamps) if timestamps else None
    window_end = max(timestamps) if timestamps else None

    return _Aggregate(
        provider_id=provider_id,
        capability_id=capability_id,
        cost_usd=cost_agg,
        latency_ms=latency_agg,
        quality=quality_agg,
        observation_count=len(group),
        window_start=window_start,
        window_end=window_end,
    )


def _apply_aggregator(values: list[Any], aggregator: _Aggregator) -> Any:
    if not values:
        if aggregator == "sum":
            return Decimal("0")
        return 0.0

    if aggregator == "sum":
        return sum(values, Decimal("0")) if isinstance(values[0], Decimal) else sum(values)

    numeric = [float(v) for v in values]
    if aggregator == "mean":
        return sum(numeric) / len(numeric)
    if aggregator == "median":
        sorted_vals = sorted(numeric)
        n = len(sorted_vals)
        mid = n // 2
        if n % 2 == 1:
            return sorted_vals[mid]
        return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2
    if aggregator == "p95":
        sorted_vals = sorted(numeric)
        n = len(sorted_vals)
        idx = max(0, math.ceil(0.95 * n) - 1)
        return sorted_vals[idx]

    return numeric[0]


def _normalize(
    values: list[float],
    *,
    direction: _Direction,
    method: _CompositeMethod,
) -> list[float]:
    if not values:
        return []

    min_val = min(values)
    max_val = max(values)

    if max_val == min_val:
        return [1.0] * len(values)

    result: list[float] = []
    for v in values:
        if direction == "lower_is_better":
            norm = (max_val - v) / (max_val - min_val)
        else:
            norm = (v - min_val) / (max_val - min_val)
        # Clamp to [0, 1] to guard against float rounding
        norm = max(0.0, min(1.0, norm))
        result.append(norm)
    return result


def _composite_score(
    cost_norm: float,
    latency_norm: float,
    quality_norm: float,
    cost_weight: float,
    latency_weight: float,
    quality_weight: float,
    *,
    method: _CompositeMethod,
) -> float:
    total_weight = cost_weight + latency_weight + quality_weight
    if total_weight == 0:
        return 0.0

    if method == "arithmetic":
        score = (
            cost_weight * cost_norm + latency_weight * latency_norm + quality_weight * quality_norm
        ) / total_weight
        return float(score)

    # geometric
    weights = [cost_weight, latency_weight, quality_weight]
    norms = [cost_norm, latency_norm, quality_norm]
    log_sum = 0.0
    for w, n in zip(weights, norms, strict=True):
        if n <= 0:
            return 0.0
        log_sum += w * math.log(n)
    return math.exp(log_sum / total_weight)


__all__ = [
    "EntityScorecard",
    "ScorecardBuilder",
    "ScorecardObservation",
    "observations_from_workloads",
]
