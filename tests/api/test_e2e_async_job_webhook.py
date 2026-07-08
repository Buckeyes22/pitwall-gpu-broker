"""Live async-job plus webhook e2e coverage against a RunPod queue endpoint."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import asyncpg
import httpx
import pytest
from pydantic import ValidationError

from pitwall.core.enums import WorkloadState
from pitwall.db.repository import (
    CapabilityRepository,
    ProviderRepository,
    WorkloadRepository,
)
from pitwall.migrations import discover_migrations
from tests.api._contract_helpers import build_app, client_for

pytestmark = [pytest.mark.live, pytest.mark.anyio]

_PG_URL_ENV = "PITWALL_TEST_DATABASE_URL"
_RUNPOD_ENDPOINT_ID = "rdhwjnr3j6b98y"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"
_POLL_TIMEOUT_S = 480.0
_POLL_INTERVAL_S = 5.0
_TERMINAL_STATUSES = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
        "TIMEOUT",
        "TIME_OUT",
    }
)
_QWEN3_INPUT = {
    "prompt": "The capital of France is",
    "sampling_params": {"max_tokens": 8, "temperature": 0},
}


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


async def _seed_qwen3_queue_provider(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        INSERT INTO pitwall.capabilities (
            id, name, version, class, cost_mode, config, source, enabled
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
        """,
        "cap_qwen3",
        "text.qwen3",
        "1.0.0",
        "llm",
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
        "prov_qwen3",
        "cap_qwen3",
        "prov_qwen3",
        "serverless_queue",
        _RUNPOD_ENDPOINT_ID,
        {
            "cost": {
                "mode": "per_second",
                "per_second_active": "0.00044",
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
        await _seed_qwen3_queue_provider(conn)

    try:
        yield pool
    finally:
        await pool.close()


def _skip_unless_live_inputs_present() -> None:
    if not os.getenv(_PG_URL_ENV, ""):
        pytest.skip(f"{_PG_URL_ENV} not set")

    runpod_api_key = os.getenv("RUNPOD_API_KEY", "")
    if not runpod_api_key or runpod_api_key == "test-key":
        pytest.skip("RUNPOD_API_KEY must be set to a real RunPod key")


async def _runpod_status_payload(
    *,
    api_key: str,
    endpoint_id: str,
    job_id: str,
) -> dict[str, Any]:
    from pitwall.runpod_client.queue import RUNPOD_API_BASE, QueueClient

    queue_client = QueueClient(api_key=api_key)
    try:
        queue_job = await queue_client.status(endpoint_id, job_id)
        return queue_job.raw
    except ValidationError:
        pass

    async with httpx.AsyncClient(
        base_url=f"{RUNPOD_API_BASE}/{endpoint_id}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=60.0,
    ) as client:
        response = await client.get(f"/status/{job_id}")
        response.raise_for_status()
        data = response.json()

    if not isinstance(data, dict):
        raise AssertionError(f"RunPod status response was not an object: {data!r}")
    return data


async def _poll_until_completed(
    *,
    api_key: str,
    endpoint_id: str,
    job_id: str,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + _POLL_TIMEOUT_S
    last_payload: dict[str, Any] | None = None

    while True:
        payload = await _runpod_status_payload(
            api_key=api_key,
            endpoint_id=endpoint_id,
            job_id=job_id,
        )
        last_payload = payload
        status = payload.get("status")
        if status == "COMPLETED":
            assert payload.get("output")
            return payload
        if status in _TERMINAL_STATUSES:
            raise AssertionError(f"RunPod job ended with {status}: {payload!r}")
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                f"RunPod job {job_id} did not complete within {_POLL_TIMEOUT_S}s; "
                f"last payload: {last_payload!r}"
            )
        await asyncio.sleep(_POLL_INTERVAL_S)


async def test_async_job_webhook_round_trips_real_runpod_queue_result(
    live_pg_pool: asyncpg.Pool,
    clear_app_module: None,
) -> None:
    _skip_unless_live_inputs_present()

    from pitwall.config import load_settings_from_env
    from pitwall.core.inference import create_and_dispatch_job
    from pitwall.reconciler import apply_terminal_status_and_publish
    from pitwall.webhook_receiver import app as receiver_app

    capability = await CapabilityRepository(live_pg_pool).get("cap_qwen3")
    provider = await ProviderRepository(live_pg_pool).get("prov_qwen3")
    assert capability is not None
    assert provider is not None

    settings = load_settings_from_env()
    workload = await create_and_dispatch_job(
        live_pg_pool,
        capability=capability,
        provider=provider,
        capability_params=_QWEN3_INPUT,
        idempotency_key=None,
        webhook_url=None,
        settings=settings,
    )
    assert workload.state == WorkloadState.QUEUED

    persisted = await WorkloadRepository(live_pg_pool).get(workload.id)
    assert persisted is not None
    runpod_job_id = persisted.runpod_job_id
    assert runpod_job_id

    status_payload = await _poll_until_completed(
        api_key=settings.runpod_api_key,
        endpoint_id=_RUNPOD_ENDPOINT_ID,
        job_id=runpod_job_id,
    )
    real_output = status_payload["output"]
    assert real_output

    receiver_app.state.pool = live_pg_pool
    receiver_app.state.redis_settings = None
    webhook_payload = {
        "id": runpod_job_id,
        "status": "COMPLETED",
        "output": real_output,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receiver_app),
        base_url="http://test",
    ) as receiver_client:
        first_delivery = await receiver_client.post(
            "/webhooks/runpod",
            json=webhook_payload,
        )
        second_delivery = await receiver_client.post(
            "/webhooks/runpod",
            json=webhook_payload,
        )

    assert first_delivery.status_code == 200, first_delivery.text
    assert first_delivery.json() == {"ok": True, "duplicate": False}
    assert second_delivery.status_code == 200, second_delivery.text
    assert second_delivery.json() == {"ok": True, "duplicate": True}

    completed_at = dt.datetime.now(dt.UTC)
    transitioned = await apply_terminal_status_and_publish(
        live_pg_pool,
        None,
        runpod_job_id,
        "COMPLETED",
        completed_at,
    )
    assert transitioned is True

    mod = build_app(pool=cast(Any, live_pg_pool))
    async with client_for(mod) as client:
        status_resp = await client.get(f"/v1/jobs/{workload.id}/status")
        result_resp = await client.get(f"/v1/jobs/{workload.id}/result")

    assert status_resp.status_code == 200, status_resp.text
    status_body = status_resp.json()
    assert status_body["id"] == workload.id
    assert status_body["state"] == WorkloadState.COMPLETED.value
    assert status_body["runpod_job_id"] == runpod_job_id

    assert result_resp.status_code == 200, result_resp.text
    assert result_resp.json() == {"id": workload.id, "result": None}
