"""Task 5c: webhook-subscriptions route contract (hermetic).

Routes: POST /v1/webhook-subscriptions (201), GET /v1/webhook-subscriptions.
Dep via Depends (overridable): _repo. Verified vs source 2026-05-30:
  - WebhookSubscriptionCreate (core/models.py) uses consumer + webhook_url
    (+ optional hmac_secret); repo.create(consumer=, webhook_url=, hmac_secret=, active=).
  - _subscription_to_response reads id/consumer/webhook_url/active/created_at/updated_at.
  - GET returns list[WebhookSubscriptionResponse] (a plain JSON array).
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.api._contract_helpers import build_app, client_for, override

pytestmark = pytest.mark.anyio
_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)


class _Sub:
    """Stand-in matching the attrs _subscription_to_response reads."""

    def __init__(self) -> None:
        self.id = "whs_x"
        self.consumer = "demo"
        self.webhook_url = "https://example.test/hook"
        self.active = True
        self.created_at = _NOW
        self.updated_at = _NOW


def _setup(clear_app_module, *, listed=None):
    repo = AsyncMock()
    repo.create.return_value = _Sub()
    repo.list.return_value = list(listed or [])
    mod = build_app(pool=MagicMock())
    from pitwall.api.routes.webhook_subscriptions import _repo

    override(mod, _repo, repo)
    return mod, repo


async def test_create_happy_201(clear_app_module, monkeypatch) -> None:
    mod, repo = _setup(clear_app_module)
    import pitwall.api.routes.webhook_subscriptions as webhook_mod

    monkeypatch.setattr(
        webhook_mod,
        "resolve_webhook_target",
        AsyncMock(return_value=SimpleNamespace(url="https://example.test/hook")),
    )
    body = {"consumer": "demo", "webhook_url": "https://example.test/hook"}
    async with client_for(mod) as client:
        resp = await client.post("/v1/webhook-subscriptions", json=body)
    assert resp.status_code == 201
    assert resp.json()["consumer"] == "demo"
    assert resp.json()["signing_secret"]
    create_kwargs = repo.create.await_args.kwargs
    assert create_kwargs["hmac_secret"] == resp.json()["signing_secret"]
    assert create_kwargs["actor"] == "rest:webhook"


async def test_create_missing_required_422(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module)
    async with client_for(mod) as client:
        resp = await client.post(
            "/v1/webhook-subscriptions", json={"webhook_url": "https://x.test/h"}
        )
    assert resp.status_code == 422


async def test_list_returns_array(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module, listed=[_Sub()])
    async with client_for(mod) as client:
        resp = await client.get("/v1/webhook-subscriptions")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["consumer"] == "demo"


async def test_method_not_allowed_405(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module)
    async with client_for(mod) as client:
        resp = await client.delete("/v1/webhook-subscriptions")
    assert resp.status_code == 405


async def test_rotate_deactivate_activate_and_delete_lifecycle(clear_app_module) -> None:
    mod, repo = _setup(clear_app_module)
    repo.rotate_secret.return_value = _Sub()
    deactivated = _Sub()
    deactivated.active = False
    repo.deactivate.return_value = deactivated
    repo.activate.return_value = _Sub()
    repo.delete.return_value = True

    async with client_for(mod) as client:
        rotated = await client.post("/v1/webhook-subscriptions/7/rotate-secret")
        disabled = await client.post("/v1/webhook-subscriptions/7/deactivate")
        enabled = await client.post("/v1/webhook-subscriptions/7/activate")
        deleted = await client.delete("/v1/webhook-subscriptions/7")

    assert rotated.status_code == 200
    assert rotated.json()["signing_secret"]
    assert disabled.status_code == 200
    assert disabled.json()["active"] is False
    assert enabled.status_code == 200
    assert enabled.json()["active"] is True
    assert deleted.status_code == 204
    repo.rotate_secret.assert_awaited_once()
    repo.deactivate.assert_awaited_once_with(7, actor="rest:webhook")
    repo.activate.assert_awaited_once_with(7, actor="rest:webhook")
    repo.delete.assert_awaited_once_with(7, actor="rest:webhook")


async def test_missing_subscription_has_stable_404(clear_app_module) -> None:
    mod, repo = _setup(clear_app_module)
    repo.rotate_secret.return_value = None
    async with client_for(mod) as client:
        response = await client.post("/v1/webhook-subscriptions/99/rotate-secret")
    assert response.status_code == 404
    assert response.json() == {"error": "webhook_subscription_not_found", "id": 99}
