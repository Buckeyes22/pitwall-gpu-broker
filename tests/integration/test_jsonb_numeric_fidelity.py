"""release program Task 6: JSONB & NUMERIC fidelity against real Postgres.

cost_*_usd are NUMERIC(12,6); input/result are JSONB. This proves a 6-decimal
Decimal round-trips EXACTLY (no float drift) and nested JSON structures survive a
write/read cycle through the real engine + JSONB codec. Uses the live pg_pool.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, ProviderType, WorkloadState
from pitwall.core.models import Capability, Provider, Workload
from pitwall.db.repository import (
    CapabilityRepository,
    ProviderRepository,
    WorkloadRepository,
)
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.anyio, pytest.mark.integration, requires_pg]
_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)


async def _seed(pool) -> None:
    await CapabilityRepository(pool).create(
        Capability(
            id="cap_bge_m3",
            name="embedding.bge-m3",
            version="1.0.0",
            class_=CapabilityClass.EMBEDDING,
            cost_mode="per_second",
            source=CapabilitySource.API,
            enabled=True,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    await ProviderRepository(pool).create(
        Provider(
            id="prov_bge_m3",
            capability_id="cap_bge_m3",
            name="prov_bge_m3",
            provider_type=ProviderType.SERVERLESS_LB,
            runpod_endpoint_id="eptest00000000",
            config={"lb_base_url": "https://x.api.runpod.ai"},
            priority=1,
            enabled=True,
            health_status="healthy",
            updated_at=_NOW,
        )
    )


async def test_numeric_decimal_roundtrips_without_float_drift(pg_pool) -> None:
    await _seed(pg_pool)
    repo = WorkloadRepository(pg_pool)
    # A value that is NOT exactly representable in binary float.
    estimate = Decimal("0.000001")  # smallest NUMERIC(12,6) unit
    await repo.insert(
        Workload(
            id="wkl_1",
            capability_id="cap_bge_m3",
            provider_id="prov_bge_m3",
            type="async",
            state=WorkloadState.QUEUED,
            submitted_at=_NOW,
            cost_estimate_usd=estimate,
        )
    )
    fetched = await repo.get("wkl_1")
    assert fetched is not None
    assert isinstance(fetched.cost_estimate_usd, Decimal)
    assert fetched.cost_estimate_usd == estimate


async def test_numeric_preserves_six_decimal_places(pg_pool) -> None:
    await _seed(pg_pool)
    repo = WorkloadRepository(pg_pool)
    estimate = Decimal("123.456789")  # 6 dp, fits NUMERIC(12,6)
    await repo.insert(
        Workload(
            id="wkl_2",
            capability_id="cap_bge_m3",
            provider_id="prov_bge_m3",
            type="async",
            state=WorkloadState.QUEUED,
            submitted_at=_NOW,
            cost_estimate_usd=estimate,
        )
    )
    fetched = await repo.get("wkl_2")
    assert fetched.cost_estimate_usd == Decimal("123.456789")


async def test_nested_jsonb_input_survives_roundtrip(pg_pool) -> None:
    await _seed(pg_pool)
    repo = WorkloadRepository(pg_pool)
    payload = {
        "texts": ["alpha", "béta", "gämma"],
        "nested": {"a": [1, 2, {"deep": True}], "b": None},
        "unicode": "emⓞji ✓",
    }
    await repo.insert(
        Workload(
            id="wkl_3",
            capability_id="cap_bge_m3",
            provider_id="prov_bge_m3",
            type="async",
            state=WorkloadState.QUEUED,
            submitted_at=_NOW,
            input=payload,
        )
    )
    fetched = await repo.get("wkl_3")
    assert fetched is not None
    assert isinstance(fetched.input, dict)
    assert fetched.input == payload
