"""Notification payloads for L14 LB hibernate sweep alerts.

When a registered LB endpoint has workersMin > 0 (indicating it should be
hibernated but isn't), this module sends an alert with the provider, endpoint,
duration, and burn estimate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pitwall.cost.notifications import NotificationResult, Notifier, get_notifier

if TYPE_CHECKING:
    import httpx

log = logging.getLogger("pitwall.cost.notifications")

L14_DAILY_BURN_PER_WORKER_USD = 100.0


@dataclass(frozen=True)
class HibernateSweepAlert:
    """Alert payload for L14 LB hibernate sweep."""

    provider_id: str
    provider_name: str
    endpoint_id: str
    workers_min: int
    duration_hours: float
    burn_estimate_usd: float


@dataclass(frozen=True)
class HibernateAlertResult:
    """Result of sending a hibernate sweep alert."""

    provider_id: str
    endpoint_id: str
    email_id: str | None
    error: str | None


def _build_alert_body(alert: HibernateSweepAlert) -> str:
    return (
        f"L14 Hibernate Sweep Alert: Endpoint needs attention\n\n"
        f"Provider: {alert.provider_name} ({alert.provider_id})\n"
        f"Endpoint: {alert.endpoint_id}\n"
        f"Workers Min: {alert.workers_min}\n"
        f"Duration: {alert.duration_hours:.1f} hours\n"
        f"Burn Estimate: ${alert.burn_estimate_usd:.2f}/day\n"
        f"Total Burn: ${alert.burn_estimate_usd * (alert.duration_hours / 24):.2f}\n"
    )


def _build_alert_subject(alert: HibernateSweepAlert) -> str:
    return (
        f"[Pitwall] L14 Alert: {alert.provider_name} endpoint {alert.endpoint_id} "
        f"has workersMin={alert.workers_min}"
    )


async def send_hibernate_sweep_alert(
    alert: HibernateSweepAlert,
    http_client: httpx.AsyncClient | None = None,
    notifier: Notifier | None = None,
) -> HibernateAlertResult:
    """Send or log a notification for an L14 hibernate sweep alert.

    Args:
        alert: The HibernateSweepAlert containing provider, endpoint,
            duration, and burn estimate.
        http_client: Deprecated; kept for compatibility with older callers.
        notifier: Optional notifier override for tests or custom transports.

    Returns:
        HibernateAlertResult with email_id on success or error message on failure.
    """
    try:
        subject = _build_alert_subject(alert)
        body = _build_alert_body(alert)

        if http_client is not None:
            log.debug("Ignoring deprecated http_client argument; notifier transport is configured")

        try:
            result = (notifier or get_notifier()).send(subject=subject, body=body)
        except Exception as exc:  # reason: notifier failure logged and recorded as failed delivery
            log.error(
                "Failed to send hibernate sweep alert for provider %s, endpoint %s: %s",
                alert.provider_id,
                alert.endpoint_id,
                exc,
            )
            result = NotificationResult(ok=False, error=str(exc))

        if result.ok:
            log.info(
                "Dispatched hibernate sweep alert for provider %s, endpoint %s, email_id=%s",
                alert.provider_id,
                alert.endpoint_id,
                result.email_id,
            )
            return HibernateAlertResult(
                provider_id=alert.provider_id,
                endpoint_id=alert.endpoint_id,
                email_id=result.email_id,
                error=None,
            )

        log.error(
            "Failed to send hibernate sweep alert for provider %s, endpoint %s: %s",
            alert.provider_id,
            alert.endpoint_id,
            result.error,
        )
        return HibernateAlertResult(
            provider_id=alert.provider_id,
            endpoint_id=alert.endpoint_id,
            email_id=None,
            error=result.error,
        )

    except Exception as exc:  # reason: notifier failure logged and recorded as failed delivery
        log.error(
            "Failed to send hibernate sweep alert for provider %s, endpoint %s: %s",
            alert.provider_id,
            alert.endpoint_id,
            exc,
        )
        return HibernateAlertResult(
            provider_id=alert.provider_id,
            endpoint_id=alert.endpoint_id,
            email_id=None,
            error=str(exc),
        )


__all__ = [
    "HibernateAlertResult",
    "HibernateSweepAlert",
    "L14_DAILY_BURN_PER_WORKER_USD",
    "send_hibernate_sweep_alert",
]
