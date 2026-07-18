"""Tests for L14 LB hibernate sweep alerts."""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest

from pitwall.cost.hibernate_alerts import (
    L14_DAILY_BURN_PER_WORKER_USD,
    HibernateAlertResult,
    HibernateSweepAlert,
    send_hibernate_sweep_alert,
)
from pitwall.cost.notifications import NotificationResult

pytestmark = pytest.mark.anyio


class _FakeNotifier:
    def __init__(self, result: NotificationResult | None = None) -> None:
        self.result = result or NotificationResult(ok=True, email_id="test_email_123")
        self.sent: list[dict[str, str]] = []

    def send(self, *, subject: str, body: str) -> NotificationResult:
        self.sent.append({"subject": subject, "body": body})
        return self.result


@pytest.fixture
def set_env_hibernate_alert() -> None:
    def _set(
        api_key: str = "re_test_key",
        from_addr: str = "alerts@example.com",
        to_addr: str = "admin@example.com",
    ) -> None:
        os.environ["RESEND_API_KEY"] = api_key
        os.environ["RESEND_SENDER_EMAIL"] = from_addr
        os.environ["RESEND_BUDGET_ALERT_EMAIL"] = to_addr

    return _set


@pytest.fixture(autouse=True)
def cleanup_env_hibernate_alert() -> None:
    yield
    for var in (
        "RESEND_API_KEY",
        "PITWALL_ALERT_FROM",
        "PITWALL_ALERT_TO",
        "RESEND_SENDER_EMAIL",
        "RESEND_BUDGET_ALERT_EMAIL",
    ):
        os.environ.pop(var, None)


async def test_hibernate_sweep_alert_payload_contains_provider(
    set_env_hibernate_alert: Any,
) -> None:
    set_env_hibernate_alert()
    notifier = _FakeNotifier()

    alert = HibernateSweepAlert(
        provider_id="prov_bge_m3_lb_us_ks",
        provider_name="bge-m3-lb-us-ks",
        endpoint_id="eptest00000000",
        workers_min=1,
        duration_hours=25.0,
        burn_estimate_usd=L14_DAILY_BURN_PER_WORKER_USD,
    )

    result = await send_hibernate_sweep_alert(alert, notifier=notifier)

    assert result.provider_id == "prov_bge_m3_lb_us_ks"
    assert result.error is None
    assert result.email_id == "test_email_123"
    assert "bge-m3-lb-us-ks" in notifier.sent[0]["body"]


async def test_hibernate_sweep_alert_payload_contains_endpoint(
    set_env_hibernate_alert: Any,
) -> None:
    set_env_hibernate_alert()
    notifier = _FakeNotifier()

    alert = HibernateSweepAlert(
        provider_id="prov_bge_m3_lb_us_ks",
        provider_name="bge-m3-lb-us-ks",
        endpoint_id="eptest00000000",
        workers_min=1,
        duration_hours=25.0,
        burn_estimate_usd=L14_DAILY_BURN_PER_WORKER_USD,
    )

    result = await send_hibernate_sweep_alert(alert, notifier=notifier)

    assert result.endpoint_id == "eptest00000000"
    assert result.error is None
    assert "eptest00000000" in notifier.sent[0]["body"]


async def test_hibernate_sweep_alert_payload_contains_duration(
    set_env_hibernate_alert: Any,
) -> None:
    set_env_hibernate_alert()
    notifier = _FakeNotifier()

    alert = HibernateSweepAlert(
        provider_id="prov_bge_m3_lb_us_ks",
        provider_name="bge-m3-lb-us-ks",
        endpoint_id="eptest00000000",
        workers_min=1,
        duration_hours=25.0,
        burn_estimate_usd=L14_DAILY_BURN_PER_WORKER_USD,
    )

    result = await send_hibernate_sweep_alert(alert, notifier=notifier)

    assert result.error is None
    assert result.email_id == "test_email_123"
    assert "25.0 hours" in notifier.sent[0]["body"]


async def test_hibernate_sweep_alert_payload_contains_burn_estimate(
    set_env_hibernate_alert: Any,
) -> None:
    set_env_hibernate_alert()
    notifier = _FakeNotifier()

    alert = HibernateSweepAlert(
        provider_id="prov_bge_m3_lb_us_ks",
        provider_name="bge-m3-lb-us-ks",
        endpoint_id="eptest00000000",
        workers_min=1,
        duration_hours=48.0,
        burn_estimate_usd=200.0,
    )

    result = await send_hibernate_sweep_alert(alert, notifier=notifier)

    assert result.error is None
    assert result.email_id == "test_email_123"
    assert "$200.00/day" in notifier.sent[0]["body"]


async def test_hibernate_sweep_alert_missing_api_key_logs_alert(
    caplog: pytest.LogCaptureFixture,
) -> None:
    for var in ("RESEND_API_KEY", "RESEND_SENDER_EMAIL", "RESEND_BUDGET_ALERT_EMAIL"):
        os.environ.pop(var, None)

    alert = HibernateSweepAlert(
        provider_id="prov_bge_m3_lb_us_ks",
        provider_name="bge-m3-lb-us-ks",
        endpoint_id="eptest00000000",
        workers_min=1,
        duration_hours=25.0,
        burn_estimate_usd=L14_DAILY_BURN_PER_WORKER_USD,
    )

    with caplog.at_level(logging.WARNING, logger="pitwall.alerts"):
        result = await send_hibernate_sweep_alert(alert)

    assert result.error is None
    assert result.email_id is None
    assert "L14 Alert" in caplog.text


async def test_hibernate_sweep_alert_http_error_returns_error(
    set_env_hibernate_alert: Any,
) -> None:
    set_env_hibernate_alert()
    notifier = _FakeNotifier(NotificationResult(ok=False, error="401 Unauthorized"))

    alert = HibernateSweepAlert(
        provider_id="prov_bge_m3_lb_us_ks",
        provider_name="bge-m3-lb-us-ks",
        endpoint_id="eptest00000000",
        workers_min=1,
        duration_hours=25.0,
        burn_estimate_usd=L14_DAILY_BURN_PER_WORKER_USD,
    )

    result = await send_hibernate_sweep_alert(alert, notifier=notifier)

    assert result.error is not None
    assert "401" in result.error
    assert result.email_id is None


def test_hibernate_sweep_alert_result_dataclass() -> None:
    result = HibernateAlertResult(
        provider_id="prov_test",
        endpoint_id="ep_test",
        email_id="email_123",
        error=None,
    )

    assert result.provider_id == "prov_test"
    assert result.endpoint_id == "ep_test"
    assert result.email_id == "email_123"
    assert result.error is None


def test_hibernate_sweep_alert_dataclass() -> None:
    alert = HibernateSweepAlert(
        provider_id="prov_bge_m3_lb_us_ks",
        provider_name="bge-m3-lb-us-ks",
        endpoint_id="eptest00000000",
        workers_min=1,
        duration_hours=25.0,
        burn_estimate_usd=100.0,
    )

    assert alert.provider_id == "prov_bge_m3_lb_us_ks"
    assert alert.provider_name == "bge-m3-lb-us-ks"
    assert alert.endpoint_id == "eptest00000000"
    assert alert.workers_min == 1
    assert alert.duration_hours == 25.0
    assert alert.burn_estimate_usd == 100.0


def test_l14_daily_burn_constant() -> None:
    assert L14_DAILY_BURN_PER_WORKER_USD == 100.0
