from __future__ import annotations

import datetime as dt
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability
from pitwall.core.models import Provider as ProviderRecord
from pitwall.cost.estimator import PerSecondPricing
from pitwall.providers.vast import VastProvider

_USD_QUANTUM = Decimal("0.000001")


def _capability() -> Capability:
    now = dt.datetime(2026, 6, 2, 12, 0, tzinfo=dt.UTC)
    return Capability(
        id="cap_gpu_lease",
        name="gpu.lease",
        version="1",
        class_=CapabilityClass.GPU_LEASE,
        cost_mode=CostMode.PER_SECOND,
        source=CapabilitySource.API,
        created_at=now,
        updated_at=now,
    )


def _provider_record(price_per_hour: Decimal, bid_price_per_hour: Decimal) -> ProviderRecord:
    return ProviderRecord(
        id="prov_vast_prop",
        capability_id="cap_gpu_lease",
        name="vast-prop",
        provider_type=ProviderType.POD_LEASE,
        config={
            "ask_id": 1,
            "create": {"image": "ubuntu:22.04"},
            "cost": {
                "kind": "per_second",
                "price_per_hour": str(price_per_hour),
                "bid_price_per_hour": str(bid_price_per_hour),
            },
        },
        priority=1,
        source=CapabilitySource.API,
        updated_at=dt.datetime(2026, 6, 2, 12, 0, tzinfo=dt.UTC),
    )


_hourly_money = st.decimals(
    min_value=Decimal("0.001"),
    max_value=Decimal("128"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)


@given(price_per_hour=_hourly_money, bid_price_per_hour=_hourly_money)
def test_vast_hourly_rates_round_trip_into_per_second_pricing(
    price_per_hour: Decimal,
    bid_price_per_hour: Decimal,
) -> None:
    capability = _capability()
    pricing = VastProvider().pricing_model(
        capability,
        _provider_record(price_per_hour, bid_price_per_hour),
    )

    assert isinstance(pricing, PerSecondPricing)
    assert (pricing.rate_per_second * Decimal(3600)).quantize(_USD_QUANTUM) == price_per_hour
    assert pricing.bid_rate_per_second is not None
    assert (pricing.bid_rate_per_second * Decimal(3600)).quantize(
        _USD_QUANTUM
    ) == bid_price_per_hour
    assert pricing.upper_bound(capability, {}) >= pricing.estimate(capability, {})
