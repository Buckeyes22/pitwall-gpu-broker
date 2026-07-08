"""Property-based tests for the pure what-if cost simulator."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability
from pitwall.cost.simulator import WhatIfSimulator
from pitwall.routing import PlanningContext, RoutingRequest

pytestmark = pytest.mark.property

_NOW = datetime(2026, 6, 2, 14, 30, tzinfo=UTC)
_CAP_ID = "cap_whatif_prop"
_CAP_NAME = "embedding.what-if.prop"

_MONEY = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)
_RATE = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)


def _capability(timeout_ms: int) -> Capability:
    return Capability(
        id=_CAP_ID,
        name=_CAP_NAME,
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode="per_second",
        defaults={"execution_timeout_ms": timeout_ms},
        created_at=_NOW,
        updated_at=_NOW,
    )


def _context(rate: Decimal, timeout_ms: int) -> PlanningContext:
    return PlanningContext.replay(
        now=_NOW,
        providers=[
            {
                "id": "prov_primary",
                "capability_id": _CAP_ID,
                "name": "prov_primary",
                "provider_type": ProviderType.SERVERLESS_QUEUE.value,
                "region": "US-KS-2",
                "config": {"cost": {"per_second_active": str(rate)}},
                "priority": 1,
                "enabled": True,
                "health_status": "healthy",
            }
        ],
        capability=_capability(timeout_ms),
    )


def _request() -> RoutingRequest:
    return RoutingRequest(capability_name=_CAP_NAME, capability_id=_CAP_ID)


@given(
    budget=_MONEY,
    current_spend=_MONEY,
    low_rate=_RATE,
    extra_rate=_RATE,
    timeout_ms=st.integers(min_value=1, max_value=86_400_000),
)
def test_budget_headroom_is_monotonic_in_selected_provider_rate(
    budget: Decimal,
    current_spend: Decimal,
    low_rate: Decimal,
    extra_rate: Decimal,
    timeout_ms: int,
) -> None:
    high_rate = low_rate + extra_rate

    low_projection = WhatIfSimulator(
        _context(low_rate, timeout_ms),
        budget_usd=budget,
        current_spend_usd=current_spend,
    ).simulate(_request())
    high_projection = WhatIfSimulator(
        _context(high_rate, timeout_ms),
        budget_usd=budget,
        current_spend_usd=current_spend,
    ).simulate(_request())

    assert high_projection.reserved_usd >= low_projection.reserved_usd
    assert high_projection.budget_headroom_usd is not None
    assert low_projection.budget_headroom_usd is not None
    assert high_projection.budget_headroom_usd <= low_projection.budget_headroom_usd
