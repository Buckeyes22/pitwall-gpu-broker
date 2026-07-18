from __future__ import annotations

from decimal import Decimal
from typing import Any

import anyio
import pytest

from pitwall.cost.budget_gate import BudgetGate
from tests.conftest import make_asyncpg_budget_pool

_BUDGET_GATE_THRESHOLD_MS = 5


@pytest.mark.benchmark
def test_budget_gate_try_launch_code_path_under_5ms(benchmark: Any) -> None:
    gate = BudgetGate(
        make_asyncpg_budget_pool(
            current_spend=Decimal("0"),
            admitted_id="wkl_test",
        ),
        monthly_budget_usd="1000",
        per_request_max_usd="100",
    )

    with anyio.from_thread.start_blocking_portal() as portal:

        async def try_launch_once() -> str:
            return await gate.try_launch(
                capability_id="cap_x",
                provider_id="prov_x",
                estimate_usd=Decimal("0.01"),
            )

        def launch_once() -> str:
            return portal.call(try_launch_once)

        result = benchmark.pedantic(launch_once, rounds=100)

    assert result == "wkl_test"
    assert benchmark.stats["median"] * 1000 <= _BUDGET_GATE_THRESHOLD_MS
