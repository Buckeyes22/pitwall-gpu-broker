"""release program Task 8: webhook dedup and Redis pub/sub against live services.

Proves WebhookDeliveryRepository uses the real Postgres uniqueness/check
constraints for RunPod webhook delivery deduplication, and that workload
completion events publish to a live Redis pub/sub subscriber.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from pitwall.core.enums import WorkloadState
from pitwall.db.repository import WebhookDeliveryRepository, WebhookDeliveryResult
from pitwall.reconciler import build_workload_completed_event, publish_workload_completed
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, requires_pg]

_WORKLOAD_COMPLETED_CHANNEL = "pitwall:workload:completed"


async def _wait_for_pubsub_message(
    pubsub: Any,
    *,
    ignore_subscribe_messages: bool,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        message = await pubsub.get_message(
            ignore_subscribe_messages=ignore_subscribe_messages,
            timeout=0.1,
        )
        if message is not None:
            return message
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for Redis pub/sub message")


async def _wait_for_subscription(pubsub: Any) -> None:
    message = await _wait_for_pubsub_message(
        pubsub,
        ignore_subscribe_messages=False,
    )
    assert message["type"] == "subscribe"
    assert message["channel"] == _WORKLOAD_COMPLETED_CHANNEL


async def test_webhook_delivery_first_insert_persists_row(pg_pool) -> None:
    repo = WebhookDeliveryRepository(pg_pool)
    payload = {"status": "COMPLETED", "job": {"id": "rp-job-first"}}

    result = await repo.insert_or_skip("rp-job-first", 1, payload)

    assert isinstance(result, WebhookDeliveryResult)
    assert result.is_new is True
    assert isinstance(result.delivery_id, int)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, runpod_job_id, attempt
            FROM pitwall.runpod_webhook_deliveries
            WHERE id = $1
            """,
            result.delivery_id,
        )
    assert row is not None
    assert row["id"] == result.delivery_id
    assert row["runpod_job_id"] == "rp-job-first"
    assert row["attempt"] == 1


async def test_webhook_delivery_duplicate_is_skipped(pg_pool) -> None:
    repo = WebhookDeliveryRepository(pg_pool)

    first = await repo.insert_or_skip("rp-job-duplicate", 1, {"status": "COMPLETED"})
    duplicate = await repo.insert_or_skip("rp-job-duplicate", 1, {"status": "FAILED"})

    assert first.is_new is True
    assert isinstance(first.delivery_id, int)
    assert duplicate.is_new is False
    assert duplicate.delivery_id is None

    async with pg_pool.acquire() as conn:
        row_count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM pitwall.runpod_webhook_deliveries
            WHERE runpod_job_id = $1 AND attempt = $2
            """,
            "rp-job-duplicate",
            1,
        )
    assert row_count == 1


async def test_webhook_delivery_different_attempt_is_new(pg_pool) -> None:
    repo = WebhookDeliveryRepository(pg_pool)

    first = await repo.insert_or_skip("rp-job-retry", 1, {"status": "IN_PROGRESS"})
    retry = await repo.insert_or_skip("rp-job-retry", 2, {"status": "COMPLETED"})

    assert first.is_new is True
    assert retry.is_new is True
    assert isinstance(first.delivery_id, int)
    assert isinstance(retry.delivery_id, int)
    assert retry.delivery_id != first.delivery_id

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, attempt
            FROM pitwall.runpod_webhook_deliveries
            WHERE runpod_job_id = $1
            ORDER BY attempt
            """,
            "rp-job-retry",
        )
    assert [(row["id"], row["attempt"]) for row in rows] == [
        (first.delivery_id, 1),
        (retry.delivery_id, 2),
    ]


async def test_webhook_delivery_attempt_check_is_enforced(pg_pool) -> None:
    repo = WebhookDeliveryRepository(pg_pool)

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await repo.insert_or_skip("rp-job-invalid-attempt", 0, {"status": "COMPLETED"})


async def test_webhook_delivery_payload_persists_as_jsonb_dict(pg_pool) -> None:
    repo = WebhookDeliveryRepository(pg_pool)
    payload = {
        "status": "COMPLETED",
        "metrics": {"execution_ms": 127, "output_bytes": 4096},
        "result": {"items": [{"id": "item-1", "score": 0.98}]},
    }

    result = await repo.insert_or_skip("rp-job-payload", 1, payload)

    assert result.is_new is True
    assert isinstance(result.delivery_id, int)

    async with pg_pool.acquire() as conn:
        stored_payload = await conn.fetchval(
            """
            SELECT payload
            FROM pitwall.runpod_webhook_deliveries
            WHERE id = $1
            """,
            result.delivery_id,
        )
    assert stored_payload == payload


async def test_publish_workload_completed_reaches_live_subscriber(redis_client) -> None:
    pubsub = redis_client.pubsub()
    completed_at = dt.datetime(2026, 5, 30, 14, 15, 16, tzinfo=dt.UTC)
    workload = {
        "id": "workload-redis-roundtrip",
        "capability_id": "cap-bge-m3",
        "provider_id": "prov-runpod-a",
        "state": WorkloadState.COMPLETED,
        "completed_at": completed_at,
        "execution_ms": 3210,
        "output_bytes": 8192,
        "cost_actual_usd": Decimal("0.012300"),
        "error": {"code": "provider_warning", "message": "completed with warning"},
    }
    event = build_workload_completed_event(workload)

    await pubsub.subscribe(_WORKLOAD_COMPLETED_CHANNEL)
    try:
        await _wait_for_subscription(pubsub)

        subscriber_count = await publish_workload_completed(redis_client, event)
        message = await _wait_for_pubsub_message(
            pubsub,
            ignore_subscribe_messages=True,
        )

        assert subscriber_count == 1
        assert message["channel"] == _WORKLOAD_COMPLETED_CHANNEL

        decoded = json.loads(message["data"])
        assert decoded == event
        assert decoded["event"] == "workload.completed"
        assert decoded["workload_id"] == "workload-redis-roundtrip"
        assert decoded["completed_at"] == completed_at.isoformat()
        assert decoded["cost_actual_usd"] == "0.012300"
        assert decoded["error"] == {
            "code": "provider_warning",
            "message": "completed with warning",
        }
    finally:
        await pubsub.unsubscribe(_WORKLOAD_COMPLETED_CHANNEL)
        await pubsub.aclose()
