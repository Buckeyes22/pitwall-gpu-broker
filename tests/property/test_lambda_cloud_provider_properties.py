from __future__ import annotations

import datetime as dt
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability
from pitwall.core.models import Provider as ProviderRecord
from pitwall.cost.estimator import PerVmSecondPricing
from pitwall.providers.lambda_cloud import LambdaCloudProvider

_USD_QUANTUM = Decimal("0.000001")


def _capability(timeout_ms: int) -> Capability:
    now = dt.datetime(2026, 6, 2, 12, 0, tzinfo=dt.UTC)
    return Capability(
        id="cap_gpu_lease",
        name="gpu.lease",
        version="1",
        class_=CapabilityClass.GPU_LEASE,
        cost_mode=CostMode.PER_SECOND,
        source=CapabilitySource.API,
        defaults={"execution_timeout_ms": timeout_ms},
        created_at=now,
        updated_at=now,
    )


def _provider_record(rate_per_second: Decimal) -> ProviderRecord:
    return ProviderRecord(
        id="prov_lambda_cloud_prop",
        capability_id="cap_gpu_lease",
        name="lambda-cloud-prop",
        provider_type=ProviderType.POD_LEASE,
        config={
            "launch": {
                "region_name": "us-west-1",
                "instance_type_name": "gpu_1x_a10",
                "ssh_key_names": ["pitwall-ci"],
            },
            "cost": {"kind": "per_vm_second", "rate_per_second": str(rate_per_second)},
        },
        priority=1,
        source=CapabilitySource.API,
        updated_at=dt.datetime(2026, 6, 2, 12, 0, tzinfo=dt.UTC),
    )


_vm_second_rates = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("8"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)
_timeouts_ms = st.integers(min_value=1, max_value=86_400_000)


@given(rate_per_second=_vm_second_rates, timeout_ms=_timeouts_ms)
def test_lambda_cloud_vm_second_pricing_is_flat_nonnegative_and_quantized(
    rate_per_second: Decimal,
    timeout_ms: int,
) -> None:
    capability = _capability(timeout_ms)
    pricing = LambdaCloudProvider().pricing_model(
        capability,
        _provider_record(rate_per_second),
    )

    assert isinstance(pricing, PerVmSecondPricing)
    assert pricing.rate_per_second == rate_per_second
    assert pricing.estimate(capability, {}) >= 0
    assert pricing.estimate(capability, {}) == pricing.upper_bound(capability, {})
    assert pricing.estimate(capability, {}) == pricing.estimate(capability, {}).quantize(
        _USD_QUANTUM
    )
