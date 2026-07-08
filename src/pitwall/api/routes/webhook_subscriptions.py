"""FastAPI route handlers for the Webhook Subscription surface.

POST   /v1/webhook-subscriptions  — consumer registers a webhook URL
GET    /v1/webhook-subscriptions  — list subscriptions (optional)

The hmac_secret is stored but never returned via the API.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from pitwall.api.exceptions import WebhookSubscriptionNotFound
from pitwall.api.schemas.params import OptionalStrQuery
from pitwall.core.models import (
    WebhookSecretRotationResponse,
    WebhookSubscription,
    WebhookSubscriptionCreate,
    WebhookSubscriptionCreated,
    WebhookSubscriptionResponse,
)
from pitwall.db.repository import WebhookSubscriptionRepository
from pitwall.webhook_dispatcher.secret_store import WebhookSecretCipher
from pitwall.webhook_dispatcher.security import redact_webhook_url, resolve_webhook_target

router = APIRouter()
SubscriptionId = Annotated[int, Path(ge=1)]


def _repo(request: Request) -> WebhookSubscriptionRepository:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "app.state.pool is not configured; "
            "ensure an asyncpg.Pool is attached to app.state before serving requests"
        )
    try:
        cipher = WebhookSecretCipher.from_env()
    except ValueError as exc:
        raise HTTPException(
            status_code=424,
            detail={
                "error": "webhook_encryption_not_configured",
                "message": "webhook secret encryption keys are not configured",
            },
        ) from exc
    return WebhookSubscriptionRepository(pool, cipher)


def _subscription_to_response(sub: WebhookSubscription) -> dict[str, Any]:
    return {
        "id": sub.id,
        "consumer": sub.consumer,
        "webhook_url": redact_webhook_url(sub.webhook_url),
        "active": sub.active,
        "created_at": sub.created_at.isoformat(),
        "updated_at": sub.updated_at.isoformat(),
    }


@router.post(
    "/v1/webhook-subscriptions",
    status_code=201,
    response_model=WebhookSubscriptionCreated,
)
async def create_subscription(
    body: WebhookSubscriptionCreate,
    repo: WebhookSubscriptionRepository = Depends(_repo),
) -> dict[str, Any]:
    target = await resolve_webhook_target(body.webhook_url)
    signing_secret = secrets.token_urlsafe(32)
    sub = await repo.create(
        consumer=body.consumer,
        webhook_url=target.url,
        hmac_secret=signing_secret,
        active=True,
        actor="rest:webhook",
    )
    return {**_subscription_to_response(sub), "signing_secret": signing_secret}


@router.get(
    "/v1/webhook-subscriptions",
    response_model=list[WebhookSubscriptionResponse],
)
async def list_subscriptions(
    request: Request,
    consumer: OptionalStrQuery = None,
    active_only: bool = False,
    repo: WebhookSubscriptionRepository = Depends(_repo),
) -> list[dict[str, Any]]:
    subs = await repo.list(consumer=consumer, active_only=active_only)
    return [_subscription_to_response(sub) for sub in subs]


@router.post(
    "/v1/webhook-subscriptions/{subscription_id}/rotate-secret",
    response_model=WebhookSecretRotationResponse,
)
async def rotate_subscription_secret(
    subscription_id: SubscriptionId,
    repo: WebhookSubscriptionRepository = Depends(_repo),
) -> dict[str, Any]:
    signing_secret = secrets.token_urlsafe(32)
    sub = await repo.rotate_secret(
        subscription_id,
        signing_secret,
        actor="rest:webhook",
    )
    if sub is None:
        raise WebhookSubscriptionNotFound(subscription_id)
    return {"id": sub.id, "signing_secret": signing_secret}


@router.post(
    "/v1/webhook-subscriptions/{subscription_id}/deactivate",
    response_model=WebhookSubscriptionResponse,
)
async def deactivate_subscription(
    subscription_id: SubscriptionId,
    repo: WebhookSubscriptionRepository = Depends(_repo),
) -> dict[str, Any]:
    sub = await repo.deactivate(subscription_id, actor="rest:webhook")
    if sub is None:
        raise WebhookSubscriptionNotFound(subscription_id)
    return _subscription_to_response(sub)


@router.post(
    "/v1/webhook-subscriptions/{subscription_id}/activate",
    response_model=WebhookSubscriptionResponse,
)
async def activate_subscription(
    subscription_id: SubscriptionId,
    repo: WebhookSubscriptionRepository = Depends(_repo),
) -> dict[str, Any]:
    sub = await repo.activate(subscription_id, actor="rest:webhook")
    if sub is None:
        raise WebhookSubscriptionNotFound(subscription_id)
    return _subscription_to_response(sub)


@router.delete(
    "/v1/webhook-subscriptions/{subscription_id}",
    status_code=204,
    response_class=Response,
)
async def delete_subscription(
    subscription_id: SubscriptionId,
    repo: WebhookSubscriptionRepository = Depends(_repo),
) -> Response:
    if not await repo.delete(subscription_id, actor="rest:webhook"):
        raise WebhookSubscriptionNotFound(subscription_id)
    return Response(status_code=204)


__all__ = ["router"]
