from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anyio
import httpx
import pytest

import pitwall.webhook_receiver as receiver

_FAST_200_THRESHOLD_MS = 50


@dataclass(frozen=True, slots=True)
class _InsertResult:
    is_new: bool


_INSERT_RESULT = _InsertResult(is_new=True)


class _FakeWebhookDeliveryRepository:
    def __init__(self, pool: object) -> None:
        self.pool = pool

    async def insert_or_skip(
        self,
        *,
        runpod_job_id: str,
        attempt: int,
        payload: dict[str, Any],
    ) -> _InsertResult:
        return _INSERT_RESULT


@pytest.mark.benchmark
def test_runpod_webhook_fast_200_path_under_50ms(
    benchmark: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(receiver, "WebhookDeliveryRepository", _FakeWebhookDeliveryRepository)
    monkeypatch.setattr(receiver, "_WEBHOOK_SECRET", "")
    receiver.app.state.pool = object()
    receiver.app.state.redis_settings = None

    payload = {"id": "job_fast_200", "status": "IN_PROGRESS"}

    with anyio.from_thread.start_blocking_portal() as portal:
        transport = httpx.ASGITransport(app=receiver.app)
        client = httpx.AsyncClient(transport=transport, base_url="http://testserver")

        async def send_post() -> httpx.Response:
            return await client.post("/webhooks/runpod", json=payload)

        def post_once() -> int:
            response = portal.call(send_post)
            assert response.status_code == 200
            assert response.json() == {"ok": True, "duplicate": False}
            return response.status_code

        try:
            benchmark.pedantic(post_once, rounds=50)
        finally:
            portal.call(client.aclose)

    assert benchmark.stats["median"] * 1000 < _FAST_200_THRESHOLD_MS
