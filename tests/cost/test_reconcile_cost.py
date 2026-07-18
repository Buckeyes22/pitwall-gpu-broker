"""Hermetic tests for provider billing truth-up against cost_daily."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.cost.reconcile_cost import (
    AsyncpgCostTruthUpRepository,
    CostReconcileWindow,
    ProviderActualCostWindow,
    RecordedCostWindow,
    reconcile_cost,
)

pytestmark = pytest.mark.anyio

_DAY = dt.date(2026, 6, 1)


def _window(
    *,
    day: dt.date = _DAY,
    capability_class: str = "embedding",
    provider_type: str = "serverless_lb",
) -> CostReconcileWindow:
    return CostReconcileWindow(
        day=day,
        capability_class=capability_class,
        provider_type=provider_type,
    )


def test_reconcile_cost_emits_positive_adjustment_for_provider_overage() -> None:
    plan = reconcile_cost(
        recorded=[
            RecordedCostWindow(
                window=_window(),
                recorded_usd=Decimal("10.000000"),
                workload_count=3,
            )
        ],
        provider_actuals=[
            ProviderActualCostWindow(
                window=_window(),
                actual_usd=Decimal("12.345678"),
                source="runpod-billing",
            )
        ],
    )

    assert len(plan.adjustments) == 1
    adjustment = plan.adjustments[0]
    assert adjustment.window == _window()
    assert adjustment.recorded_usd == Decimal("10.000000")
    assert adjustment.provider_actual_usd == Decimal("12.345678")
    assert adjustment.adjustment_usd == Decimal("2.345678")
    assert adjustment.direction == "increase"
    assert adjustment.sources == ("runpod-billing",)
    assert plan.total_adjustment_usd == Decimal("2.345678")


def test_reconcile_cost_emits_negative_adjustment_for_provider_underrun() -> None:
    plan = reconcile_cost(
        recorded=[
            RecordedCostWindow(
                window=_window(),
                recorded_usd=Decimal("12.000000"),
                workload_count=2,
            )
        ],
        provider_actuals=[
            ProviderActualCostWindow(
                window=_window(),
                actual_usd=Decimal("9.250000"),
                source="runpod-billing",
            )
        ],
    )

    assert len(plan.adjustments) == 1
    adjustment = plan.adjustments[0]
    assert adjustment.adjustment_usd == Decimal("-2.750000")
    assert adjustment.direction == "decrease"
    assert plan.total_adjustment_usd == Decimal("-2.750000")


def test_reconcile_cost_omits_windows_within_tolerance() -> None:
    plan = reconcile_cost(
        recorded=[
            RecordedCostWindow(
                window=_window(),
                recorded_usd=Decimal("10.000000"),
                workload_count=1,
            )
        ],
        provider_actuals=[
            ProviderActualCostWindow(
                window=_window(),
                actual_usd=Decimal("10.000001"),
                source="runpod-billing",
            )
        ],
        tolerance_usd=Decimal("0.000001"),
    )

    assert plan.adjustments == ()
    assert plan.total_adjustment_usd == Decimal("0.000000")
    assert plan.window_count == 1


def test_reconcile_cost_uses_recorded_provider_window_union() -> None:
    provider_only = _window(capability_class="llm")
    recorded_only = _window(provider_type="lambda_cloud")

    plan = reconcile_cost(
        recorded=[
            RecordedCostWindow(
                window=recorded_only,
                recorded_usd=Decimal("5.000000"),
                workload_count=1,
            )
        ],
        provider_actuals=[
            ProviderActualCostWindow(
                window=provider_only,
                actual_usd=Decimal("7.500000"),
                source="lambda-billing",
            )
        ],
    )

    assert [item.window for item in plan.adjustments] == [recorded_only, provider_only]
    assert [item.adjustment_usd for item in plan.adjustments] == [
        Decimal("-5.000000"),
        Decimal("7.500000"),
    ]


def test_reconcile_cost_groups_duplicates_and_sorts_deterministically() -> None:
    first = _window(day=dt.date(2026, 6, 1), capability_class="embedding")
    second = _window(day=dt.date(2026, 6, 2), capability_class="llm")

    plan = reconcile_cost(
        recorded=[
            RecordedCostWindow(window=second, recorded_usd=Decimal("1.000000")),
            RecordedCostWindow(window=first, recorded_usd=Decimal("2.000000")),
            RecordedCostWindow(window=first, recorded_usd=Decimal("0.500000")),
        ],
        provider_actuals=[
            ProviderActualCostWindow(
                window=first,
                actual_usd=Decimal("4.000000"),
                source="runpod-b",
            ),
            ProviderActualCostWindow(
                window=second,
                actual_usd=Decimal("1.250000"),
                source="runpod-a",
            ),
            ProviderActualCostWindow(
                window=first,
                actual_usd=Decimal("0.500000"),
                source="runpod-a",
            ),
        ],
    )

    assert [item.window for item in plan.adjustments] == [first, second]
    assert plan.adjustments[0].recorded_usd == Decimal("2.500000")
    assert plan.adjustments[0].provider_actual_usd == Decimal("4.500000")
    assert plan.adjustments[0].sources == ("runpod-a", "runpod-b")
    assert plan.adjustments[1].adjustment_usd == Decimal("0.250000")


def test_reconcile_cost_quantizes_money_to_six_decimal_places() -> None:
    plan = reconcile_cost(
        recorded=[
            RecordedCostWindow(
                window=_window(),
                recorded_usd=Decimal("1.0000004"),
            )
        ],
        provider_actuals=[
            ProviderActualCostWindow(
                window=_window(),
                actual_usd=Decimal("1.0000015"),
                source="runpod-billing",
            )
        ],
    )

    assert plan.adjustments[0].recorded_usd == Decimal("1.000000")
    assert plan.adjustments[0].provider_actual_usd == Decimal("1.000002")
    assert plan.adjustments[0].adjustment_usd == Decimal("0.000002")


def test_reconcile_plan_serializes_decimals_as_strings() -> None:
    plan = reconcile_cost(
        recorded=[RecordedCostWindow(window=_window(), recorded_usd=Decimal("3"))],
        provider_actuals=[
            ProviderActualCostWindow(
                window=_window(),
                actual_usd=Decimal("4.25"),
                source="runpod-billing",
            )
        ],
    )

    assert plan.to_serializable_dict() == {
        "window_count": 1,
        "adjustment_count": 1,
        "total_adjustment_usd": "1.250000",
        "adjustments": [
            {
                "day": "2026-06-01",
                "capability_class": "embedding",
                "provider_type": "serverless_lb",
                "recorded_usd": "3.000000",
                "provider_actual_usd": "4.250000",
                "adjustment_usd": "1.250000",
                "direction": "increase",
                "sources": ["runpod-billing"],
            }
        ],
    }


def test_money_inputs_must_be_decimal() -> None:
    with pytest.raises(TypeError, match="recorded_usd must be Decimal"):
        RecordedCostWindow(window=_window(), recorded_usd=1.25)
    with pytest.raises(TypeError, match="actual_usd must be Decimal"):
        ProviderActualCostWindow(window=_window(), actual_usd=1.25, source="runpod")
    with pytest.raises(TypeError, match="tolerance_usd must be Decimal"):
        reconcile_cost(recorded=[], provider_actuals=[], tolerance_usd=0.01)


@dataclass
class _PoolAndConnection:
    pool: MagicMock
    conn: MagicMock


def _mock_pool(rows: Iterable[dict[str, Any]]) -> _PoolAndConnection:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=list(rows))
    conn.executemany = AsyncMock(return_value=None)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return _PoolAndConnection(pool=pool, conn=conn)


async def test_asyncpg_repository_reads_and_applies_cost_daily_adjustments() -> None:
    pair = _mock_pool(
        [
            {
                "day": _DAY,
                "capability_class": "embedding",
                "provider_type": "serverless_lb",
                "workload_count": 2,
                "cost_usd": Decimal("1.500000"),
            }
        ]
    )
    repo = AsyncpgCostTruthUpRepository(pair.pool)

    recorded = await repo.fetch_recorded_windows(
        start_day=_DAY,
        end_day=dt.date(2026, 6, 2),
    )
    plan = reconcile_cost(
        recorded=recorded,
        provider_actuals=[
            ProviderActualCostWindow(
                window=_window(),
                actual_usd=Decimal("2.000000"),
                source="runpod-billing",
            )
        ],
    )
    applied = await repo.apply_adjustments(plan.adjustments)

    assert recorded == (
        RecordedCostWindow(
            window=_window(),
            recorded_usd=Decimal("1.500000"),
            workload_count=2,
        ),
    )
    assert applied == 1
    fetch_sql = pair.conn.fetch.await_args.args[0]
    assert "FROM pitwall.cost_daily" in fetch_sql
    assert "day >= $1" in fetch_sql
    assert "day < $2" in fetch_sql
    upsert_sql = pair.conn.executemany.await_args.args[0]
    assert "INSERT INTO pitwall.cost_daily" in upsert_sql
    assert "cost_usd = EXCLUDED.cost_usd" in upsert_sql
    assert pair.conn.executemany.await_args.args[1] == [
        (_DAY, "embedding", "serverless_lb", Decimal("2.000000"))
    ]
