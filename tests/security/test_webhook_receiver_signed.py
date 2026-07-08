"""S5b: opt-in HMAC verification on the inbound RunPod webhook (find→fix).

With ``PITWALL_WEBHOOK_SECRET`` set, the receiver must accept only requests
carrying a valid ``X-Pitwall-Webhook-Signature`` produced by the shared
constant-time signer, and reject missing / wrong-secret / tampered-body /
replayed (stale-timestamp) deliveries. A static guard pins that the receiver
*delegates* to the shared constant-time verifier rather than rolling its own
comparison.
"""

from __future__ import annotations

import inspect
import json
import time
from typing import Any

import httpx
import pytest

from pitwall.webhook_dispatcher.signer import sign
from tests.security.conftest import WEBHOOK_SECRET

pytestmark = pytest.mark.security

_SIG_HEADER = "X-Pitwall-Webhook-Signature"
_BODY = json.dumps({"id": "job-7", "status": "IN_PROGRESS"}).encode()


async def _post(module: Any, body: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=module.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/webhooks/runpod",
            content=body,
            headers={"content-type": "application/json", **(headers or {})},
        )


@pytest.mark.anyio
async def test_valid_signature_accepted(webhook_app_builder: Any) -> None:
    module = webhook_app_builder(secret=WEBHOOK_SECRET)
    sig = sign(_BODY, WEBHOOK_SECRET)
    resp = await _post(module, _BODY, {_SIG_HEADER: sig})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.anyio
async def test_missing_signature_rejected(webhook_app_builder: Any) -> None:
    module = webhook_app_builder(secret=WEBHOOK_SECRET)
    resp = await _post(module, _BODY)
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_wrong_secret_signature_rejected(webhook_app_builder: Any) -> None:
    module = webhook_app_builder(secret=WEBHOOK_SECRET)
    sig = sign(_BODY, "a-different-secret-entirely")
    resp = await _post(module, _BODY, {_SIG_HEADER: sig})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_tampered_body_rejected(webhook_app_builder: Any) -> None:
    module = webhook_app_builder(secret=WEBHOOK_SECRET)
    sig = sign(_BODY, WEBHOOK_SECRET)  # signed over _BODY...
    tampered = json.dumps({"id": "job-7", "status": "COMPLETED"}).encode()  # ...sent another
    resp = await _post(module, tampered, {_SIG_HEADER: sig})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_replayed_stale_timestamp_rejected(webhook_app_builder: Any) -> None:
    module = webhook_app_builder(secret=WEBHOOK_SECRET)
    stale_ts = int(time.time()) - 400  # outside the signer's 300s window
    sig = sign(_BODY, WEBHOOK_SECRET, timestamp=stale_ts)
    resp = await _post(module, _BODY, {_SIG_HEADER: sig})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_previous_secret_is_accepted_during_rotation(webhook_app_builder: Any) -> None:
    old_secret = "old-webhook-secret-for-rotation"
    module = webhook_app_builder(
        secret=WEBHOOK_SECRET,
        previous_secrets=[old_secret],
    )
    response = await _post(module, _BODY, {_SIG_HEADER: sign(_BODY, old_secret)})
    assert response.status_code == 200


@pytest.mark.anyio
async def test_oversized_body_rejected_before_signature_work(webhook_app_builder: Any) -> None:
    module = webhook_app_builder(secret=WEBHOOK_SECRET, max_body_bytes=16)
    body = json.dumps({"payload": "x" * 100}).encode()
    response = await _post(module, body, {_SIG_HEADER: sign(body, WEBHOOK_SECRET)})
    assert response.status_code == 413
    assert response.json()["detail"] == "webhook body too large"


@pytest.mark.anyio
async def test_non_json_content_type_rejected(webhook_app_builder: Any) -> None:
    module = webhook_app_builder(secret=WEBHOOK_SECRET)
    transport = httpx.ASGITransport(app=module.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/runpod",
            content=_BODY,
            headers={"content-type": "text/plain", _SIG_HEADER: sign(_BODY, WEBHOOK_SECRET)},
        )
    assert response.status_code == 415


@pytest.mark.anyio
async def test_malformed_or_non_object_json_rejected(webhook_app_builder: Any) -> None:
    module = webhook_app_builder(secret=WEBHOOK_SECRET)
    malformed = b"{not-json"
    malformed_response = await _post(
        module,
        malformed,
        {_SIG_HEADER: sign(malformed, WEBHOOK_SECRET)},
    )
    array_body = b"[]"
    array_response = await _post(
        module,
        array_body,
        {_SIG_HEADER: sign(array_body, WEBHOOK_SECRET)},
    )
    assert malformed_response.status_code == 400
    assert array_response.status_code == 400


@pytest.mark.anyio
async def test_webhook_rate_limit_returns_stable_429(webhook_app_builder: Any) -> None:
    module = webhook_app_builder(secret=WEBHOOK_SECRET, rate_limit="2/60s")
    signature = sign(_BODY, WEBHOOK_SECRET)
    first = await _post(module, _BODY, {_SIG_HEADER: signature})
    second = await _post(module, _BODY, {_SIG_HEADER: signature})
    limited = await _post(module, _BODY, {_SIG_HEADER: signature})
    assert first.status_code == 200
    assert second.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "30"


def test_receiver_delegates_to_constant_time_verifier() -> None:
    """The route must verify via the shared constant-time signer (find→fix)."""
    import pitwall.webhook_receiver as wr
    from pitwall.webhook_dispatcher import signer

    assert "hmac.compare_digest" in inspect.getsource(signer.verify), (
        "the shared signer.verify must use a constant-time comparison"
    )
    route_src = inspect.getsource(wr.runpod_webhook)
    assert "verify_signature" in route_src, (
        "runpod_webhook must delegate signature checks to the shared verifier"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10/500ms", (10, 0.5)),
        ("10/2s", (10, 2.0)),
        ("10/3m", (10, 180.0)),
        ("10/4", (10, 4.0)),
    ],
)
def test_webhook_rate_limit_parser_supports_documented_units(
    raw: str,
    expected: tuple[int, float],
) -> None:
    import pitwall.webhook_receiver as receiver

    assert receiver._parse_rate_limit(raw) == expected


@pytest.mark.parametrize("raw", ["10", "0/1s", "1/0s", "bad/1s"])
def test_webhook_rate_limit_parser_rejects_invalid_values(raw: str) -> None:
    import pitwall.webhook_receiver as receiver

    with pytest.raises(ValueError):
        receiver._parse_rate_limit(raw)
