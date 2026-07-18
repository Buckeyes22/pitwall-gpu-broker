"""Property tests for provider cost truth-up invariants."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.cost.reconcile_cost import (
    CostReconcileWindow,
    ProviderActualCostWindow,
    RecordedCostWindow,
    reconcile_cost,
)

pytestmark = pytest.mark.property


_DAY = dt.date(2026, 6, 1)

decimal_usd = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
).map(lambda value: Decimal(str(value)))


@given(recorded=decimal_usd, actual=decimal_usd)
def test_adjustment_total_equals_provider_minus_recorded(
    recorded: Decimal,
    actual: Decimal,
) -> None:
    window = CostReconcileWindow(
        day=_DAY,
        capability_class="embedding",
        provider_type="serverless_lb",
    )

    plan = reconcile_cost(
        recorded=[RecordedCostWindow(window=window, recorded_usd=recorded)],
        provider_actuals=[
            ProviderActualCostWindow(
                window=window,
                actual_usd=actual,
                source="runpod-billing",
            )
        ],
    )

    assert plan.total_adjustment_usd == actual - recorded


@given(
    records=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=5),
            st.sampled_from(["embedding", "llm"]),
            st.sampled_from(["serverless_lb", "lambda_cloud"]),
            decimal_usd,
            decimal_usd,
        ),
        min_size=0,
        max_size=20,
    )
)
def test_reconcile_output_is_order_invariant(
    records: list[tuple[int, str, str, Decimal, Decimal]],
) -> None:
    recorded: list[RecordedCostWindow] = []
    actuals: list[ProviderActualCostWindow] = []
    for day_offset, capability_class, provider_type, recorded_usd, actual_usd in records:
        window = CostReconcileWindow(
            day=_DAY + dt.timedelta(days=day_offset),
            capability_class=capability_class,
            provider_type=provider_type,
        )
        recorded.append(RecordedCostWindow(window=window, recorded_usd=recorded_usd))
        actuals.append(
            ProviderActualCostWindow(
                window=window,
                actual_usd=actual_usd,
                source=f"{provider_type}-billing",
            )
        )

    forward = reconcile_cost(recorded=recorded, provider_actuals=actuals)
    backward = reconcile_cost(
        recorded=list(reversed(recorded)),
        provider_actuals=list(reversed(actuals)),
    )

    assert backward == forward
