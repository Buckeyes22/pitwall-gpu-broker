"""S5a: the inbound RunPod webhook authenticates nothing by default.

This pins the current insecure default — with ``PITWALL_WEBHOOK_SECRET`` unset,
any unsigned POST is accepted (200). It must stay green after S5 lands so the
HMAC gate is provably *opt-in* (unset secret = unchanged behaviour).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.security


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/webhooks/runpod", "/runpod"])
async def test_unsigned_webhook_accepted_when_secret_unset(
    webhook_app_builder: Any, path: str
) -> None:
    module = webhook_app_builder(secret=None)

    transport = httpx.ASGITransport(app=module.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(path, json={"id": "job-1", "status": "IN_PROGRESS"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
