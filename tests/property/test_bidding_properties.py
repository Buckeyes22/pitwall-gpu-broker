"""Property-based tests for the spot-market bidding engine."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.finops.bidding import BiddingEngine, BiddingPolicy, SpotPrice, SpotPriceSnapshot

pytestmark = pytest.mark.property

_NOW = dt.datetime(2026, 6, 2, 15, 0, tzinfo=dt.UTC)

prices = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("2"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
).map(lambda value: Decimal(str(value)))


@given(first=prices, second=prices, third=prices)
def test_selected_bid_never_exceeds_effective_max_and_picks_cheapest_eligible(
    first: Decimal,
    second: Decimal,
    third: Decimal,
) -> None:
    max_price = Decimal("1.000000")
    snapshot = SpotPriceSnapshot(
        observed_at=_NOW,
        prices=(
            SpotPrice(
                provider_id="prov_c",
                resource_id="slot_c",
                gpu="NVIDIA L4",
                minimum_bid_usd_per_hour=third,
            ),
            SpotPrice(
                provider_id="prov_a",
                resource_id="slot_a",
                gpu="NVIDIA L4",
                minimum_bid_usd_per_hour=first,
            ),
            SpotPrice(
                provider_id="prov_b",
                resource_id="slot_b",
                gpu="NVIDIA L4",
                minimum_bid_usd_per_hour=second,
            ),
        ),
    )

    plan = BiddingEngine().evaluate(
        snapshot,
        BiddingPolicy(
            target_price_usd_per_hour=Decimal("0.500000"),
            max_price_usd_per_hour=max_price,
        ),
    )

    selected = plan.selected_actions
    eligible = sorted(
        (
            (price, provider_id)
            for price, provider_id in [
                (first, "prov_a"),
                (second, "prov_b"),
                (third, "prov_c"),
            ]
            if price <= max_price
        ),
        key=lambda item: (item[0], item[1]),
    )
    if not eligible:
        assert selected == ()
        assert plan.blocked is True
        return

    assert len(selected) == 1
    action = selected[0]
    assert action.provider_id == eligible[0][1]
    assert action.bid_usd_per_hour <= max_price
    assert action.bid_usd_per_hour >= min(Decimal("0.500000"), max_price)
