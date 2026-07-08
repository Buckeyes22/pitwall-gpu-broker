"""Notification transports for cost and budget alerts."""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from pitwall.cost.threshold_alerts import ThresholdCrossing

log = logging.getLogger("pitwall.cost.notifications")
alert_log = logging.getLogger("pitwall.alerts")

RESEND_API_KEY_ENV = "RESEND_API_KEY"
ALERT_FROM_ENV = "PITWALL_ALERT_FROM"
ALERT_TO_ENV = "PITWALL_ALERT_TO"
LEGACY_ALERT_FROM_ENV = "RESEND_SENDER_EMAIL"
LEGACY_ALERT_TO_ENV = "RESEND_BUDGET_ALERT_EMAIL"


@dataclass(frozen=True)
class NotificationResult:
    threshold_pct: int | None = None
    email_id: str | None = None
    error: str | None = None
    ok: bool = True


class Notifier(Protocol):
    def send(self, *, subject: str, body: str) -> NotificationResult:
        """Send or record an alert notification."""


class LogNotifier:
    """Out-of-the-box notifier that records alerts in application logs."""

    def send(self, *, subject: str, body: str) -> NotificationResult:
        alert_log.warning("Pitwall alert: %s\n%s", subject, body)
        return NotificationResult(ok=True)


class ResendNotifier:
    """Notifier backed by the Resend Python SDK.

    The SDK is imported only when an alert is actually sent so the base package
    can be installed without the email extra.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._sender = sender
        self._recipient = recipient

    def send(self, *, subject: str, body: str) -> NotificationResult:
        try:
            api_key = self._api_key or os.environ.get(RESEND_API_KEY_ENV, "")
            if not api_key:
                return NotificationResult(
                    ok=False,
                    error=f"{RESEND_API_KEY_ENV} environment variable is not set",
                )

            sender = self._sender or _get_alert_sender()
            if not sender:
                return NotificationResult(
                    ok=False,
                    error=(
                        f"{ALERT_FROM_ENV} environment variable is not set "
                        f"(fallback {LEGACY_ALERT_FROM_ENV} is also unset)"
                    ),
                )

            recipient = self._recipient or _get_alert_recipient()
            if not recipient:
                return NotificationResult(
                    ok=False,
                    error=(
                        f"{ALERT_TO_ENV} environment variable is not set "
                        f"(fallback {LEGACY_ALERT_TO_ENV} is also unset)"
                    ),
                )

            resend: Any = importlib.import_module("resend")
            resend.api_key = api_key

            params: dict[str, Any] = {
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "text": body,
            }
            result = resend.Emails.send(params)
            email_id = _extract_email_id(result)
            log.info("Sent alert email via Resend, email_id=%s", email_id)
            return NotificationResult(ok=True, email_id=email_id)
        except ModuleNotFoundError as exc:
            if exc.name == "resend" or "resend" in str(exc):
                error = "resend package is not installed; install pitwall[email] to enable email"
            else:
                error = str(exc)
            log.error("Failed to send alert email via Resend: %s", error)
            return NotificationResult(ok=False, error=error)
        except (
            Exception
        ) as exc:  # reason: unexpected Resend failure becomes a failed NotificationResult
            log.error("Failed to send alert email via Resend: %s", exc)
            return NotificationResult(ok=False, error=str(exc))


def get_notifier() -> Notifier:
    """Return the configured notifier, defaulting to log-only alerts."""
    if os.environ.get(RESEND_API_KEY_ENV):
        return ResendNotifier()
    return LogNotifier()


def _get_alert_sender() -> str:
    return _get_first_env(ALERT_FROM_ENV, LEGACY_ALERT_FROM_ENV)


def _get_alert_recipient() -> str:
    return _get_first_env(ALERT_TO_ENV, LEGACY_ALERT_TO_ENV)


def _get_first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _extract_email_id(result: object) -> str | None:
    if isinstance(result, Mapping):
        value = result.get("id")
        return str(value) if value is not None else None
    value = getattr(result, "id", None)
    return str(value) if value is not None else None


def _build_email_body(crossing: ThresholdCrossing) -> str:
    return (
        f"Budget Alert: {crossing.threshold_pct}% threshold crossed\n\n"
        f"Spend: ${crossing.spend_usd:.2f}\n"
        f"Budget: ${crossing.budget_usd:.2f}\n"
        f"Percent: {crossing.budget_pct:.1f}%\n"
        f"Threshold: {crossing.threshold_pct}%"
    )


def _build_email_subject(crossing: ThresholdCrossing) -> str:
    return f"[Pitwall] Budget alert: {crossing.budget_pct:.1f}% of monthly budget"


def send_threshold_email(
    crossing: ThresholdCrossing,
    *,
    notifier: Notifier | None = None,
) -> NotificationResult:
    """Send or log a notification for a threshold crossing.

    The threshold evaluation remains in ``threshold_alerts.py``; this function
    only adapts the crossing into transport-neutral notification text.
    """
    subject = _build_email_subject(crossing)
    body = _build_email_body(crossing)

    try:
        result = (notifier or get_notifier()).send(subject=subject, body=body)
    except Exception as exc:  # reason: notifier failure logged and returned as failed result
        log.error(
            "Failed to send budget alert notification for %d%% threshold: %s",
            crossing.threshold_pct,
            exc,
        )
        result = NotificationResult(ok=False, error=str(exc))

    return replace(result, threshold_pct=crossing.threshold_pct)


__all__ = [
    "ALERT_FROM_ENV",
    "ALERT_TO_ENV",
    "LEGACY_ALERT_FROM_ENV",
    "LEGACY_ALERT_TO_ENV",
    "LogNotifier",
    "NotificationResult",
    "Notifier",
    "RESEND_API_KEY_ENV",
    "ResendNotifier",
    "get_notifier",
    "send_threshold_email",
]
