"""Consumer webhook dispatcher with signed delivery and bounded retries.

Dispatches completion payloads to registered consumer webhook endpoints with
HMAC-SHA256 signing and exponential backoff retry semantics. Delivery failures
are recorded separately from workload state to avoid polluting workload state
with transient delivery issues.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
import uuid
from typing import Any

from pitwall.webhook_dispatcher.security import (
    WebhookTargetRejected,
    post_pinned_https,
    resolve_webhook_target,
)
from pitwall.webhook_dispatcher.signer import sign

log = logging.getLogger("pitwall.webhook_dispatcher")

DEFAULT_RETRY_DELAYS = (0.0, 1.0, 3.0, 9.0)
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS = 4


class DeliveryOutcome:
    """Result of a webhook delivery attempt."""

    def __init__(
        self,
        success: bool,
        attempt: int,
        status_code: int | None = None,
        error_message: str | None = None,
        next_retry_at: dt.datetime | None = None,
        delivery_id: str | None = None,
    ) -> None:
        self.success = success
        self.attempt = attempt
        self.status_code = status_code
        self.error_message = error_message
        self.next_retry_at = next_retry_at
        self.delivery_id = delivery_id

    @property
    def state(self) -> str:
        if self.success:
            return "delivered"
        if self.should_retry:
            return "retry_scheduled"
        return "terminal_failure"

    @property
    def should_retry(self) -> bool:
        if self.success:
            return False
        if self.attempt >= MAX_ATTEMPTS:
            return False
        return (
            self.status_code is None
            or self.status_code in {408, 425, 429}
            or self.status_code >= 500
        )


async def _send_webhook_with_retry(
    webhook_url: str,
    payload: dict[str, Any],
    hmac_secret: str | None,
    retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    delivery_id: str | None = None,
) -> DeliveryOutcome:
    """Send a webhook with bounded retries and return the outcome.

    Args:
        webhook_url: URL to POST the webhook to.
        payload: JSON-serializable payload to send.
        hmac_secret: HMAC secret for signing, or None for unsigned delivery.
        retry_delays: Tuple of delays between attempts in seconds.
        timeout_seconds: HTTP request timeout.

    Returns:
        DeliveryOutcome describing the result of the delivery attempt.
    """
    body = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    delivery_id = delivery_id or str(uuid.uuid4())
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "X-Pitwall-Delivery-ID": delivery_id,
    }

    if hmac_secret:
        signature = sign(body, hmac_secret)
        headers["X-Pitwall-Signature"] = signature

    delays = retry_delays[:MAX_ATTEMPTS]
    last_error: str | None = None
    last_status_code: int | None = None

    for attempt_idx, delay in enumerate(delays):
        attempt = attempt_idx + 1

        if delay > 0:
            await asyncio.sleep(delay + random.uniform(0, delay * 0.2))

        try:
            target = await resolve_webhook_target(webhook_url)
            status_code = await post_pinned_https(target, body, headers, timeout_seconds)
            last_status_code = status_code
            if 200 <= status_code < 300:
                return DeliveryOutcome(
                    success=True,
                    attempt=attempt,
                    status_code=status_code,
                    delivery_id=delivery_id,
                )
            if status_code not in {408, 425, 429} and status_code < 500:
                return DeliveryOutcome(
                    success=False,
                    attempt=attempt,
                    status_code=status_code,
                    error_message=f"Non-retryable HTTP status: {status_code}",
                    delivery_id=delivery_id,
                )
            last_error = f"Retryable HTTP status: {status_code}"
        except WebhookTargetRejected:
            return DeliveryOutcome(
                success=False,
                attempt=attempt,
                status_code=None,
                error_message="Webhook target rejected by egress policy",
                delivery_id=delivery_id,
            )
        except (TimeoutError, OSError):
            last_error = "Webhook delivery transport failure"

    next_retry_at = None
    return DeliveryOutcome(
        success=False,
        attempt=len(delays),
        status_code=last_status_code,
        error_message=last_error or "Webhook delivery failed",
        next_retry_at=next_retry_at,
        delivery_id=delivery_id,
    )


async def dispatch_completion(
    workload_id: str,
    consumer: str,
    payload: dict[str, Any],
    subscriptions: list[tuple[int, str, str | None]],
    retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Dispatch a completion payload to all registered webhook subscriptions.

    Args:
        workload_id: The workload ID this completion is for.
        consumer: The consumer identifier.
        payload: The completion payload to send.
        subscriptions: List of (subscription_id, webhook_url, hmac_secret) tuples.
        retry_delays: Tuple of delays between attempts in seconds.
        timeout_seconds: HTTP request timeout.

    Returns:
        Dict with dispatch results keyed by subscription_id.
    """
    results: dict[str, Any] = {}

    for subscription_id, webhook_url, hmac_secret in subscriptions:
        sub_id_str = str(subscription_id)
        delivery_id = str(uuid.uuid4())
        event = build_completion_event(
            workload_id=workload_id,
            consumer=consumer,
            payload=payload,
            delivery_id=delivery_id,
        )
        outcome = await _send_webhook_with_retry(
            webhook_url=webhook_url,
            payload=event,
            hmac_secret=hmac_secret,
            retry_delays=retry_delays,
            timeout_seconds=timeout_seconds,
            delivery_id=delivery_id,
        )
        results[sub_id_str] = {
            "success": outcome.success,
            "attempt": outcome.attempt,
            "status_code": outcome.status_code,
            "error_message": outcome.error_message,
            "next_retry_at": outcome.next_retry_at.isoformat() if outcome.next_retry_at else None,
            "delivery_id": outcome.delivery_id,
            "state": outcome.state,
        }

    return results


def build_completion_event(
    *,
    workload_id: str,
    consumer: str,
    payload: dict[str, Any],
    delivery_id: str,
    occurred_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Build the versioned public completion envelope."""

    timestamp = occurred_at or dt.datetime.now(dt.UTC)
    return {
        "version": "1",
        "event": "workload.completed",
        "delivery_id": delivery_id,
        "occurred_at": timestamp.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
        "workload_id": workload_id,
        "consumer": consumer,
        "data": payload,
    }


__all__ = [
    "DEFAULT_RETRY_DELAYS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_ATTEMPTS",
    "DeliveryOutcome",
    "build_completion_event",
    "dispatch_completion",
]
