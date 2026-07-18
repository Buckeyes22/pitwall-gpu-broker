"""Provider billing truth-up for Pitwall cost ledgers.

The pure reconciler compares broker-recorded spend against provider-reported
actual billing for the same ``cost_daily`` window. It emits structured
adjustments that can be inspected, exported, or applied by the thin asyncpg
adapter below.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol

import asyncpg

_USD_QUANTUM = Decimal("0.000001")
_ZERO_USD = Decimal("0.000000")

_FETCH_RECORDED_SQL = """
    SELECT day, capability_class, provider_type, workload_count, cost_usd
    FROM pitwall.cost_daily
    WHERE day >= $1
      AND day < $2
    ORDER BY day, capability_class, provider_type
"""

_APPLY_ADJUSTMENT_SQL = """
    INSERT INTO pitwall.cost_daily
        (day, capability_class, provider_type, workload_count, cost_usd)
    VALUES ($1, $2, $3, 0, $4)
    ON CONFLICT (day, capability_class, provider_type)
    DO UPDATE SET
        cost_usd = EXCLUDED.cost_usd
"""

AdjustmentDirection = Literal["increase", "decrease"]


@dataclass(frozen=True, slots=True, order=True)
class CostReconcileWindow:
    """One ``pitwall.cost_daily`` billing window."""

    day: dt.date
    capability_class: str
    provider_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.day, dt.date) or isinstance(self.day, dt.datetime):
            raise TypeError("day must be datetime.date")
        object.__setattr__(
            self,
            "capability_class",
            _non_empty_string(self.capability_class, "capability_class"),
        )
        object.__setattr__(
            self,
            "provider_type",
            _non_empty_string(self.provider_type, "provider_type"),
        )


@dataclass(frozen=True, slots=True)
class RecordedCostWindow:
    """Broker-recorded cost for one reconciliation window."""

    window: CostReconcileWindow
    recorded_usd: Decimal
    workload_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.window, CostReconcileWindow):
            raise TypeError("window must be CostReconcileWindow")
        object.__setattr__(
            self,
            "recorded_usd",
            _non_negative_usd(self.recorded_usd, "recorded_usd"),
        )
        if not isinstance(self.workload_count, int) or isinstance(self.workload_count, bool):
            raise TypeError("workload_count must be int")
        if self.workload_count < 0:
            raise ValueError("workload_count must be non-negative")


@dataclass(frozen=True, slots=True)
class ProviderActualCostWindow:
    """Provider-reported actual cost for one reconciliation window."""

    window: CostReconcileWindow
    actual_usd: Decimal
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.window, CostReconcileWindow):
            raise TypeError("window must be CostReconcileWindow")
        object.__setattr__(self, "actual_usd", _non_negative_usd(self.actual_usd, "actual_usd"))
        object.__setattr__(self, "source", _non_empty_string(self.source, "source"))


@dataclass(frozen=True, slots=True)
class CostReconcileAdjustment:
    """One ledger correction required to match provider actual billing."""

    window: CostReconcileWindow
    recorded_usd: Decimal
    provider_actual_usd: Decimal
    adjustment_usd: Decimal
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.window, CostReconcileWindow):
            raise TypeError("window must be CostReconcileWindow")
        object.__setattr__(
            self,
            "recorded_usd",
            _non_negative_usd(self.recorded_usd, "recorded_usd"),
        )
        object.__setattr__(
            self,
            "provider_actual_usd",
            _non_negative_usd(self.provider_actual_usd, "provider_actual_usd"),
        )
        object.__setattr__(
            self,
            "adjustment_usd",
            _finite_usd(self.adjustment_usd, "adjustment_usd"),
        )
        sources = tuple(sorted({_non_empty_string(source, "source") for source in self.sources}))
        object.__setattr__(self, "sources", sources)

    @property
    def direction(self) -> AdjustmentDirection:
        """Return whether the ledger needs to increase or decrease."""
        if self.adjustment_usd > 0:
            return "increase"
        return "decrease"

    def to_serializable_dict(self) -> dict[str, str | list[str]]:
        """Return a stdlib-JSON-safe dict with Decimal values as strings."""
        return {
            "day": self.window.day.isoformat(),
            "capability_class": self.window.capability_class,
            "provider_type": self.window.provider_type,
            "recorded_usd": str(self.recorded_usd),
            "provider_actual_usd": str(self.provider_actual_usd),
            "adjustment_usd": str(self.adjustment_usd),
            "direction": self.direction,
            "sources": list(self.sources),
        }


@dataclass(frozen=True, slots=True)
class CostReconcilePlan:
    """Deterministic set of cost ledger corrections."""

    adjustments: tuple[CostReconcileAdjustment, ...]
    window_count: int

    def __post_init__(self) -> None:
        adjustments = tuple(self.adjustments)
        for adjustment in adjustments:
            if not isinstance(adjustment, CostReconcileAdjustment):
                raise TypeError("adjustments must contain CostReconcileAdjustment")
        object.__setattr__(self, "adjustments", adjustments)
        if not isinstance(self.window_count, int) or isinstance(self.window_count, bool):
            raise TypeError("window_count must be int")
        if self.window_count < 0:
            raise ValueError("window_count must be non-negative")

    @property
    def adjustment_count(self) -> int:
        """Number of emitted ledger corrections."""
        return len(self.adjustments)

    @property
    def total_adjustment_usd(self) -> Decimal:
        """Signed sum of all emitted corrections."""
        total = _ZERO_USD
        for adjustment in self.adjustments:
            total = _usd(total + adjustment.adjustment_usd)
        return total

    def to_serializable_dict(self) -> dict[str, int | str | list[dict[str, str | list[str]]]]:
        """Return a stdlib-JSON-safe dict with Decimal values as strings."""
        return {
            "window_count": self.window_count,
            "adjustment_count": self.adjustment_count,
            "total_adjustment_usd": str(self.total_adjustment_usd),
            "adjustments": [item.to_serializable_dict() for item in self.adjustments],
        }


class CostTruthUpRepository(Protocol):
    """Repository seam for reading and correcting cost ledger windows."""

    async def fetch_recorded_windows(
        self,
        *,
        start_day: dt.date,
        end_day: dt.date,
    ) -> tuple[RecordedCostWindow, ...]: ...

    async def apply_adjustments(
        self,
        adjustments: Iterable[CostReconcileAdjustment],
    ) -> int: ...


class AsyncpgCostTruthUpRepository:
    """Thin asyncpg adapter for ``pitwall.cost_daily`` truth-up."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def fetch_recorded_windows(
        self,
        *,
        start_day: dt.date,
        end_day: dt.date,
    ) -> tuple[RecordedCostWindow, ...]:
        """Read recorded cost windows from ``pitwall.cost_daily``."""
        _validate_date_window(start_day, end_day)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_FETCH_RECORDED_SQL, start_day, end_day)
        return tuple(_recorded_from_row(row) for row in rows)

    async def apply_adjustments(
        self,
        adjustments: Iterable[CostReconcileAdjustment],
    ) -> int:
        """Apply corrections by setting ``cost_daily.cost_usd`` to provider actuals."""
        params = [
            (
                adjustment.window.day,
                adjustment.window.capability_class,
                adjustment.window.provider_type,
                adjustment.provider_actual_usd,
            )
            for adjustment in adjustments
        ]
        if not params:
            return 0
        async with self._pool.acquire() as conn:
            await conn.executemany(_APPLY_ADJUSTMENT_SQL, params)
        return len(params)


def reconcile_cost(
    *,
    recorded: Iterable[RecordedCostWindow],
    provider_actuals: Iterable[ProviderActualCostWindow],
    tolerance_usd: Decimal = _ZERO_USD,
) -> CostReconcilePlan:
    """Compare recorded and provider-actual costs and emit ledger corrections.

    Windows are grouped by ``(day, capability_class, provider_type)``. Duplicate
    rows are summed, missing sides are treated as zero, and emitted adjustments
    are sorted by window for deterministic output.
    """
    tolerance = _non_negative_usd(tolerance_usd, "tolerance_usd")
    recorded_by_window = _aggregate_recorded(recorded)
    actual_by_window, sources_by_window = _aggregate_actuals(provider_actuals)
    windows = sorted(set(recorded_by_window) | set(actual_by_window))
    adjustments: list[CostReconcileAdjustment] = []
    for window in windows:
        recorded_usd = recorded_by_window.get(window, _ZERO_USD)
        provider_actual_usd = actual_by_window.get(window, _ZERO_USD)
        adjustment_usd = _usd(provider_actual_usd - recorded_usd)
        if abs(adjustment_usd) <= tolerance:
            continue
        adjustments.append(
            CostReconcileAdjustment(
                window=window,
                recorded_usd=recorded_usd,
                provider_actual_usd=provider_actual_usd,
                adjustment_usd=adjustment_usd,
                sources=tuple(sorted(sources_by_window.get(window, ()))),
            )
        )
    return CostReconcilePlan(adjustments=tuple(adjustments), window_count=len(windows))


async def truth_up_cost_daily(
    repository: CostTruthUpRepository,
    *,
    start_day: dt.date,
    end_day: dt.date,
    provider_actuals: Iterable[ProviderActualCostWindow],
    tolerance_usd: Decimal = _ZERO_USD,
) -> CostReconcilePlan:
    """Read recorded windows, compute corrections, apply them, and return the plan."""
    recorded = await repository.fetch_recorded_windows(start_day=start_day, end_day=end_day)
    plan = reconcile_cost(
        recorded=recorded,
        provider_actuals=provider_actuals,
        tolerance_usd=tolerance_usd,
    )
    await repository.apply_adjustments(plan.adjustments)
    return plan


def _aggregate_recorded(
    windows: Iterable[RecordedCostWindow],
) -> dict[CostReconcileWindow, Decimal]:
    totals: dict[CostReconcileWindow, Decimal] = {}
    for item in windows:
        if not isinstance(item, RecordedCostWindow):
            raise TypeError("recorded must contain RecordedCostWindow")
        totals[item.window] = _usd(totals.get(item.window, _ZERO_USD) + item.recorded_usd)
    return totals


def _aggregate_actuals(
    windows: Iterable[ProviderActualCostWindow],
) -> tuple[dict[CostReconcileWindow, Decimal], dict[CostReconcileWindow, set[str]]]:
    totals: dict[CostReconcileWindow, Decimal] = {}
    sources: dict[CostReconcileWindow, set[str]] = {}
    for item in windows:
        if not isinstance(item, ProviderActualCostWindow):
            raise TypeError("provider_actuals must contain ProviderActualCostWindow")
        totals[item.window] = _usd(totals.get(item.window, _ZERO_USD) + item.actual_usd)
        sources.setdefault(item.window, set()).add(item.source)
    return totals, sources


def _recorded_from_row(row: Mapping[str, Any]) -> RecordedCostWindow:
    return RecordedCostWindow(
        window=CostReconcileWindow(
            day=_date_value(row["day"], "day"),
            capability_class=_string_value(row["capability_class"], "capability_class"),
            provider_type=_string_value(row["provider_type"], "provider_type"),
        ),
        recorded_usd=_decimal_value(row["cost_usd"], "cost_usd"),
        workload_count=_int_value(row["workload_count"], "workload_count"),
    )


def _validate_date_window(start_day: dt.date, end_day: dt.date) -> None:
    if not isinstance(start_day, dt.date) or isinstance(start_day, dt.datetime):
        raise TypeError("start_day must be datetime.date")
    if not isinstance(end_day, dt.date) or isinstance(end_day, dt.datetime):
        raise TypeError("end_day must be datetime.date")
    if end_day <= start_day:
        raise ValueError("end_day must be after start_day")


def _date_value(value: object, name: str) -> dt.date:
    if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
        raise TypeError(f"{name} must be datetime.date")
    return value


def _string_value(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    return value


def _int_value(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int")
    return value


def _decimal_value(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    return value


def _non_empty_string(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{name} must be non-empty")
    return stripped


def _non_negative_usd(value: object, name: str) -> Decimal:
    money = _finite_usd(value, name)
    if money < 0:
        raise ValueError(f"{name} must be non-negative")
    return money


def _finite_usd(value: object, name: str) -> Decimal:
    money = _decimal_value(value, name)
    if not money.is_finite():
        raise ValueError(f"{name} must be finite")
    return _usd(money)


def _usd(value: Decimal) -> Decimal:
    return value.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)


__all__ = [
    "AdjustmentDirection",
    "AsyncpgCostTruthUpRepository",
    "CostReconcileAdjustment",
    "CostReconcilePlan",
    "CostReconcileWindow",
    "CostTruthUpRepository",
    "ProviderActualCostWindow",
    "RecordedCostWindow",
    "reconcile_cost",
    "truth_up_cost_daily",
]
