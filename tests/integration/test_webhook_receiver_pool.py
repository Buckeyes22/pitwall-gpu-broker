"""Webhook receiver against a real Postgres pool.

Regression: the receiver's lifespan used to build a bare ``asyncpg.create_pool``
without the shared jsonb codec, so persisting the delivery payload (a dict into
a jsonb column) raised ``TypeError: expected str, got dict`` and every signed
delivery returned 500. The receiver must use the shared ``pitwall.db`` pool.
"""

from __future__ import annotations

import httpx
import pytest

from pitwall.webhook_dispatcher.signer import sign

from .conftest import requires_pg

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, requires_pg]

_SECRET = "integration-webhook-secret"


async def test_signed_delivery_persists_payload_dict(pg_pool, monkeypatch) -> None:
    import pitwall.webhook_receiver as wr

    monkeypatch.setattr(wr, "_WEBHOOK_SECRET", _SECRET)
    wr.app.state.pool = pg_pool
    wr.app.state.redis_settings = None

    body = b'{"id": "job-wh-pool-regression", "status": "COMPLETED"}'
    signature = sign(body, _SECRET)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wr.app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/webhooks/runpod",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Pitwall-Webhook-Signature": signature,
            },
        )
        replay = await client.post(
            "/webhooks/runpod",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Pitwall-Webhook-Signature": signature,
            },
        )

    assert first.status_code == 200, first.text
    assert first.json() == {"ok": True, "duplicate": False}
    assert replay.status_code == 200, replay.text
    assert replay.json() == {"ok": True, "duplicate": True}

    row = await pg_pool.fetchrow(
        "SELECT payload FROM pitwall.runpod_webhook_deliveries WHERE runpod_job_id = $1",
        "job-wh-pool-regression",
    )
    assert row is not None
    assert row["payload"]["status"] == "COMPLETED"
