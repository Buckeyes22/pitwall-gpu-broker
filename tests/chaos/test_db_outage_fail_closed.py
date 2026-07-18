"""Chaos: DB outage makes budget admission fail closed."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pitwall.cost.budget_gate import BudgetGate

pytestmark = [pytest.mark.anyio, pytest.mark.chaos]


class _DeadAcquire:
    async def __aenter__(self) -> None:
        raise ConnectionError("postgres unreachable")

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _DeadPool:
    """Pool whose acquire context raises, modeling a Postgres outage."""

    def acquire(self) -> _DeadAcquire:
        return _DeadAcquire()


def _gate() -> BudgetGate:
    return BudgetGate(
        _DeadPool(),
        monthly_budget_usd=Decimal("100.00"),
        per_request_max_usd=Decimal("50.00"),
    )


async def test_try_launch_fails_closed_on_outage() -> None:
    with pytest.raises(ConnectionError):
        await _gate().try_launch(
            capability_id="cap-1",
            provider_id="prov-1",
            estimate_usd=Decimal("1.00"),
        )


async def test_current_mtd_spend_fails_closed_on_outage() -> None:
    with pytest.raises(ConnectionError):
        await _gate().current_mtd_spend()
