"""release program Task 2: repository round-trips against real Postgres.

Proves CapabilityRepository / ProviderRepository persist and re-read domain
models through real asyncpg + the JSONB codec (config decodes to dict, not str),
covering create/get/get_by_name/list/patch/enable/disable. Uses the live pg_pool.
"""

from __future__ import annotations

import datetime as dt

import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.db.repository import CapabilityRepository, ProviderRepository
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, requires_pg]
_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)


def _capability(cap_id: str = "cap_bge_m3", name: str = "embedding.bge-m3") -> Capability:
    return Capability(
        id=cap_id,
        name=name,
        version="1.0.0",
        class_=CapabilityClass.EMBEDDING,
        cost_mode="per_second",
        source=CapabilitySource.API,
        enabled=True,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider(prov_id: str = "prov_bge_m3", cap_id: str = "cap_bge_m3") -> Provider:
    return Provider(
        id=prov_id,
        capability_id=cap_id,
        name=prov_id,
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id="eptest00000000",
        config={
            "lb_base_url": "https://eptest00000000.api.runpod.ai",
            "cost": {"mode": "per_second", "per_second_active": "0.000123"},
        },
        priority=1,
        enabled=True,
        health_status="healthy",
        updated_at=_NOW,
    )


# ---- Capability ----------------------------------------------------------
async def test_capability_create_get_roundtrip(pg_pool) -> None:
    repo = CapabilityRepository(pg_pool)
    created = await repo.create(_capability())
    assert created.id == "cap_bge_m3"
    fetched = await repo.get("cap_bge_m3")
    assert fetched is not None
    assert fetched.name == "embedding.bge-m3"
    assert fetched.class_ == CapabilityClass.EMBEDDING


async def test_capability_get_by_name(pg_pool) -> None:
    repo = CapabilityRepository(pg_pool)
    await repo.create(_capability())
    fetched = await repo.get_by_name("embedding.bge-m3")
    assert fetched is not None and fetched.id == "cap_bge_m3"


async def test_capability_get_missing_returns_none(pg_pool) -> None:
    repo = CapabilityRepository(pg_pool)
    assert await repo.get("cap_nope") is None


async def test_capability_enable_disable(pg_pool) -> None:
    repo = CapabilityRepository(pg_pool)
    await repo.create(_capability())
    disabled = await repo.disable("cap_bge_m3")
    assert disabled is not None and disabled.enabled is False
    enabled = await repo.enable("cap_bge_m3")
    assert enabled is not None and enabled.enabled is True


async def test_capability_list_enabled_only(pg_pool) -> None:
    repo = CapabilityRepository(pg_pool)
    await repo.create(_capability("cap_a", "cap.a"))
    await repo.create(_capability("cap_b", "cap.b"))
    await repo.disable("cap_b")
    enabled = await repo.list(enabled_only=True)
    ids = {c.id for c in enabled}
    assert "cap_a" in ids
    assert "cap_b" not in ids


# ---- Provider (JSONB config round-trip) ----------------------------------
async def test_provider_create_get_jsonb_roundtrip(pg_pool) -> None:
    cap_repo = CapabilityRepository(pg_pool)
    await cap_repo.create(_capability())
    prov_repo = ProviderRepository(pg_pool)
    created = await prov_repo.create(_provider())
    assert created.id == "prov_bge_m3"

    fetched = await prov_repo.get("prov_bge_m3")
    assert fetched is not None
    # JSONB config must decode to a real dict, not a string.
    assert isinstance(fetched.config, dict)
    assert fetched.config["lb_base_url"] == "https://eptest00000000.api.runpod.ai"
    assert fetched.config["cost"]["per_second_active"] == "0.000123"
    assert fetched.provider_type == ProviderType.SERVERLESS_LB


async def test_provider_list_by_capability(pg_pool) -> None:
    cap_repo = CapabilityRepository(pg_pool)
    await cap_repo.create(_capability())
    prov_repo = ProviderRepository(pg_pool)
    await prov_repo.create(_provider("prov_1"))
    await prov_repo.create(_provider("prov_2"))
    listed = await prov_repo.list(capability_id="cap_bge_m3")
    assert {p.id for p in listed} == {"prov_1", "prov_2"}


async def test_provider_patch_priority(pg_pool) -> None:
    cap_repo = CapabilityRepository(pg_pool)
    await cap_repo.create(_capability())
    prov_repo = ProviderRepository(pg_pool)
    await prov_repo.create(_provider())
    patched = await prov_repo.patch("prov_bge_m3", priority=9)
    assert patched is not None and patched.priority == 9
