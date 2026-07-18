"""Live end-to-end coverage for the pod lease lifecycle against RunPod."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import asyncpg
import pytest

from pitwall.api.leases.teardown import run_teardown
from pitwall.db import _register_codecs
from pitwall.migrations import discover_migrations
from pitwall.runpod_client.pods import get_pods, terminate_pod
from tests.api._contract_helpers import build_app, client_for

pytestmark = [pytest.mark.live, pytest.mark.anyio]

_PG_URL_ENV = "PITWALL_TEST_DATABASE_URL"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"

_CAPABILITY_ID = "cap_pod"
_CAPABILITY_NAME = "pod.nginx"
_PROVIDER_ID = "prov_pod"
_LIVE_RUN_ID = os.getenv("PITWALL_LIVE_RUN_ID", "local")
if not re.fullmatch(r"[A-Za-z0-9_-]+", _LIVE_RUN_ID):
    raise ValueError("PITWALL_LIVE_RUN_ID contains unsafe pod-name characters")
_PROVIDER_NAME = f"prov_pod_acceptance_{_LIVE_RUN_ID}"
_POD_NAME_PREFIX = f"pitwall-{_PROVIDER_NAME}-"
# nginx:alpine is a tiny public image that listens on :80 and returns 200 on "/"
# immediately, so the readiness probe (GET {pod}-80.proxy.runpod.net/ -> 2xx)
# passes fast. This test validates pitwall's LEASE ORCHESTRATION (create ->
# persist -> readiness -> active -> stop -> teardown), not a GPU workload, so a
# trivial HTTP server is the honest stand-in. A heavy runpod/pytorch image just
# 502s the proxy (nothing serves :8888) and never reaches readiness.
_IMAGE_REF = "nginx:alpine"
# Short canonical GPU list (migration 0002 names) so a capacity/readiness failure
# burns at most ~timeout * len(list); SECURE cloud has more reliable stock than
# COMMUNITY spot. max_cost_per_hr still caps the rate regardless of which lands.
_GPU_PRIORITY = [
    "NVIDIA RTX A4000",
    "NVIDIA RTX A4500",
    "NVIDIA A40",
]

_TERMINAL_STATES = {"stopped", "failed", "expired"}
_ACTIVEISH_STATES = {"creating", "waiting_runtime", "waiting_probe", "active"}
_R2_ENV_KEYS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "R2_ACCESS_KEY",
    "R2_SECRET_KEY",
    "R2_SESSION_TOKEN",
    "R2_CREDENTIAL_EXPIRES_AT",
    "R2_CREDENTIAL_TTL_SECONDS",
    "R2_ENDPOINT",
    "R2_BUCKET_STAGING",
)


def _all_migration_sql() -> str:
    records = discover_migrations(_MIGRATION_DIR)
    return "\n".join((_MIGRATION_DIR / record.filename).read_text() for record in records)


async def _register_json_codec(conn: asyncpg.Connection) -> None:
    await _register_codecs(conn)
    await conn.execute("SET search_path TO pitwall, public")


async def _apply_all_migrations(conn: asyncpg.Connection) -> None:
    await conn.execute("DROP SCHEMA IF EXISTS pitwall CASCADE")
    await conn.execute(_all_migration_sql())


async def _seed_pod_lease_provider(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        INSERT INTO pitwall.capabilities (
            id, name, version, class, cost_mode, config, source, enabled
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
        """,
        _CAPABILITY_ID,
        _CAPABILITY_NAME,
        "1.0.0",
        "gpu_lease",
        "per_second",
        {},
        "api",
        True,
    )
    await conn.execute(
        """
        INSERT INTO pitwall.providers (
            id, capability_id, name, provider_type, runpod_endpoint_id,
            cloud_type, config, priority, enabled, health_status,
            consecutive_failures, cooldown_trips
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12)
        """,
        _PROVIDER_ID,
        _CAPABILITY_ID,
        _PROVIDER_NAME,
        "pod_lease",
        None,
        "SECURE",
        {
            "image_ref": _IMAGE_REF,
            "gpu_type_priority": _GPU_PRIORITY,
            "container_disk_gb": 10,
            "ports": {"http": [80]},
            "max_cost_per_hr": 0.50,
            "lease_ttl_ms": 600000,
            "cost": {
                "mode": "per_second",
                "per_second_active": "0.00012",
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
        await _seed_pod_lease_provider(conn)

    try:
        yield pool
    finally:
        await pool.close()


def _response_id(body: dict[str, Any], primary: str, fallback: str) -> str:
    value = body.get(primary) or body.get(fallback)
    assert isinstance(value, str) and value
    return value


async def _latest_persisted_lease_ids(
    pool: asyncpg.Pool,
) -> tuple[str | None, str | None]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, runpod_pod_id
            FROM pitwall.leases
            WHERE provider_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            _PROVIDER_ID,
        )
    if row is None:
        return None, None
    return str(row["id"]), str(row["runpod_pod_id"])


async def _matching_runpod_pod_ids() -> list[str]:
    pods = await get_pods()
    pod_ids: list[str] = []
    for pod in pods:
        pod_id = pod.get("id")
        name = pod.get("name")
        if (
            isinstance(pod_id, str)
            and pod_id
            and isinstance(name, str)
            and name.startswith(_POD_NAME_PREFIX)
        ):
            pod_ids.append(pod_id)
    return pod_ids


async def _best_effort_cleanup(
    *,
    pool: asyncpg.Pool,
    lease_id: str | None,
    pod_id: str | None,
) -> None:
    if lease_id is not None:
        with suppress(Exception):
            await run_teardown(lease_id, pool=pool, reason="live_e2e_finally")

    pod_ids = [pod_id] if pod_id is not None else []
    with suppress(Exception):
        pod_ids.extend(await _matching_runpod_pod_ids())

    killed: set[str] = set()
    for candidate_pod_id in pod_ids:
        if candidate_pod_id in killed:
            continue
        killed.add(candidate_pod_id)
        with suppress(Exception):
            await terminate_pod(candidate_pod_id)


async def test_live_lease_lifecycle_provisions_and_tears_down_real_runpod_pod(
    live_pg_pool: asyncpg.Pool,
    clear_app_module: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runpod_api_key = os.getenv("RUNPOD_API_KEY", "")
    if not runpod_api_key or runpod_api_key == "test-key":
        pytest.skip("RUNPOD_API_KEY must be set to a real RunPod key")

    spend_cap = os.getenv("PITWALL_LIVE_SPEND_CAP_USD", "")
    if not spend_cap:
        pytest.fail("PITWALL_LIVE_SPEND_CAP_USD is required for live acceptance")
    monkeypatch.setenv("PITWALL_MONTHLY_BUDGET_USD", spend_cap)
    monkeypatch.setenv("PITWALL_PER_REQUEST_MAX_USD", spend_cap)
    monkeypatch.setenv("R2_TEMP_CREDENTIALS_ENABLED", "false")
    monkeypatch.setenv("PITWALL_R2_TEMP_CREDENTIALS_ENABLED", "false")
    for key in _R2_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    lease_id: str | None = None
    pod_id: str | None = None
    mod = build_app(pool=cast(Any, live_pg_pool))

    try:
        async with client_for(mod) as client:
            create_resp = await client.post(
                "/v1/leases",
                json={
                    "capability_id": _CAPABILITY_NAME,
                    "provider_id": _PROVIDER_ID,
                },
                timeout=900.0,
            )
            assert create_resp.status_code == 201, create_resp.text
            create_body = create_resp.json()
            assert isinstance(create_body, dict)
            lease_id = _response_id(create_body, "lease_id", "id")
            pod_id = _response_id(create_body, "pod_id", "runpod_pod_id")

            get_resp = await client.get(f"/v1/leases/{lease_id}", timeout=30.0)
            assert get_resp.status_code == 200, get_resp.text
            get_body = get_resp.json()
            assert get_body["state"] in _ACTIVEISH_STATES
            assert get_body["state"] not in _TERMINAL_STATES
            assert get_body["runpod_pod_id"] == pod_id
            assert get_body["provider_id"] == _PROVIDER_ID

            stop_resp = await client.post(
                f"/v1/leases/{lease_id}/stop",
                json={"reason": "live_e2e_stop"},
                timeout=180.0,
            )
            assert stop_resp.status_code == 200, stop_resp.text
            stop_body = stop_resp.json()
            assert stop_body["state"] == "stopped"
            assert stop_body["runpod_pod_id"] == pod_id
    finally:
        if lease_id is None or pod_id is None:
            discovered_lease_id, discovered_pod_id = await _latest_persisted_lease_ids(live_pg_pool)
            lease_id = lease_id or discovered_lease_id
            pod_id = pod_id or discovered_pod_id
        await _best_effort_cleanup(pool=live_pg_pool, lease_id=lease_id, pod_id=pod_id)
