"""Live end-to-end coverage for POST /v1/inference against RunPod LB embed."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import asyncpg
import pytest

from pitwall.migrations import discover_migrations
from tests.api._contract_helpers import build_app, client_for

pytestmark = [pytest.mark.live, pytest.mark.anyio]

_PG_URL_ENV = "PITWALL_TEST_DATABASE_URL"
_LB_ENDPOINT_ENV = "PITWALL_LIVE_LB_ENDPOINT_ID"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"


def _all_migration_sql() -> str:
    records = discover_migrations(_MIGRATION_DIR)
    return "\n".join((_MIGRATION_DIR / record.filename).read_text() for record in records)


async def _register_json_codec(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=json.dumps,
        decoder=json.loads,
        format="text",
    )


async def _apply_all_migrations(conn: asyncpg.Connection) -> None:
    await conn.execute("DROP SCHEMA IF EXISTS pitwall CASCADE")
    await conn.execute(_all_migration_sql())


async def _seed_bge_m3(conn: asyncpg.Connection) -> None:
    endpoint_id = os.getenv(_LB_ENDPOINT_ENV, "")
    if not endpoint_id:
        pytest.fail(f"{_LB_ENDPOINT_ENV} is required for live acceptance")
    await conn.execute(
        """
        INSERT INTO pitwall.capabilities (
            id, name, version, class, cost_mode, config, source, enabled
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
        """,
        "cap_bge_m3",
        "embedding.bge-m3",
        "1.0.0",
        "embedding",
        "per_second",
        {},
        "api",
        True,
    )
    await conn.execute(
        """
        INSERT INTO pitwall.providers (
            id, capability_id, name, provider_type, runpod_endpoint_id, config,
            priority, enabled, health_status, consecutive_failures, cooldown_trips
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11)
        """,
        "prov_bge_m3",
        "cap_bge_m3",
        "prov_bge_m3",
        "serverless_lb",
        endpoint_id,
        {
            "lb_base_url": f"https://{endpoint_id}.api.runpod.ai",
            "cost": {
                "mode": "per_second",
                "per_second_active": "0.000123",
            },
        },
        1,
        True,
        "healthy",
        0,
        0,
    )


@pytest.fixture
async def live_pg_pool() -> AsyncIterator[asyncpg.Pool]:
    pg_url = os.getenv(_PG_URL_ENV, "")
    if not pg_url:
        pytest.skip(f"{_PG_URL_ENV} not set")

    pool = await asyncpg.create_pool(
        pg_url,
        min_size=1,
        max_size=4,
        init=_register_json_codec,
    )
    assert pool is not None
    async with pool.acquire() as conn:
        await _apply_all_migrations(conn)
        await _seed_bge_m3(conn)

    try:
        yield pool
    finally:
        await pool.close()


async def test_sync_inference_round_trips_real_runpod_and_persists_workload(
    live_pg_pool: asyncpg.Pool,
    clear_app_module: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runpod_api_key = os.getenv("RUNPOD_API_KEY", "")
    if not runpod_api_key or runpod_api_key == "test-key":
        pytest.skip("RUNPOD_API_KEY must be set to a real RunPod key")

    monkeypatch.delenv("PITWALL_EMBEDDING_VIA_PITWALL", raising=False)
    monkeypatch.delenv("PITWALL_BASE_URL", raising=False)

    mod = build_app(pool=cast(Any, live_pg_pool))
    async with client_for(mod) as client:
        resp = await client.post(
            "/v1/inference",
            json={
                "capability_id": "embedding.bge-m3",
                "texts": ["pitwall live e2e"],
                "return_dense": True,
                "return_sparse": False,
                "return_colbert": False,
            },
            timeout=400.0,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    result = body["result"]
    dense = result["dense"]
    assert isinstance(dense, list)
    assert len(dense) == 1
    vector = dense[0]
    assert isinstance(vector, list)
    assert len(vector) == 1024
    assert all(isinstance(value, float) for value in vector)
    assert result["sparse"] is None
    assert result["colbert"] is None

    workload_id = resp.headers["X-Pitwall-Workload-ID"]
    assert resp.headers["X-Pitwall-Capability"] == "embedding.bge-m3"
    assert body["workload_id"] == workload_id

    async with live_pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT state, cost_actual_usd
            FROM pitwall.workloads
            WHERE id = $1
            """,
            workload_id,
        )

    assert row is not None
    assert row["state"] == "completed"
    actual_cost = row["cost_actual_usd"]
    assert actual_cost is None or actual_cost >= Decimal("0")
