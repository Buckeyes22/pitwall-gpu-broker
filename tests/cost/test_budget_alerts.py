"""Tests for budget threshold alerts (80% notification)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.cost.alerts import (
    check_and_send_budget_alert,
)
from pitwall.cost.notifications import NotificationResult

pytestmark = pytest.mark.anyio


class _FakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._expiry: dict[str, int] = {}

    def exists(self, key: str) -> int:
        return 1 if key in self._data else 0

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value
        if ex is not None:
            self._expiry[key] = ex


class _FakeNotifier:
    def __init__(self, result: NotificationResult | None = None) -> None:
        self.result = result or NotificationResult(ok=True, email_id="test_email_123")
        self.sent: list[dict[str, str]] = []

    def send(self, *, subject: str, body: str) -> NotificationResult:
        self.sent.append({"subject": subject, "body": body})
        return self.result


def _mock_pool(spend: Decimal) -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()

    async def mock_fetchrow(sql: str, *args: object) -> dict[str, object]:
        if "SUM(cost_actual_usd)" in sql:
            return {"total": spend}
        return {}

    conn.fetchrow = mock_fetchrow

    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acq)
    pool.conn = conn
    return pool


@pytest.fixture
def set_env_budget_alert() -> None:
    def _set(
        budget: str = "100.0",
    ) -> None:
        os.environ["PITWALL_MONTHLY_BUDGET_USD"] = budget

    return _set


@pytest.fixture(autouse=True)
def cleanup_env_budget_alert() -> None:
    yield
    for var in (
        "PITWALL_MONTHLY_BUDGET_USD",
        "RESEND_API_KEY",
        "PITWALL_ALERT_FROM",
        "PITWALL_ALERT_TO",
        "RESEND_SENDER_EMAIL",
        "RESEND_BUDGET_ALERT_EMAIL",
    ):
        os.environ.pop(var, None)


async def test_below_threshold_returns_no_alert(
    set_env_budget_alert: Any,
) -> None:
    pool = _mock_pool(Decimal("50.00"))
    redis = _FakeRedis()
    set_env_budget_alert(budget="100.0")

    result = await check_and_send_budget_alert(
        pool,
        redis,
        now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
    )

    assert result.threshold_pct == 80
    assert result.budget_pct == 50.0
    assert result.email_sent is False
    assert result.skipped_duplicate is False


async def test_at_80_percent_triggers_alert(
    set_env_budget_alert: Any,
) -> None:
    pool = _mock_pool(Decimal("80.00"))
    redis = _FakeRedis()
    set_env_budget_alert(budget="100.0")
    notifier = _FakeNotifier()

    result = await check_and_send_budget_alert(
        pool,
        redis,
        now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        notifier=notifier,
    )

    assert result.threshold_pct == 80
    assert result.budget_pct == 80.0
    assert result.email_sent is True
    assert result.email_id == "test_email_123"
    assert result.skipped_duplicate is False
    assert len(notifier.sent) == 1


async def test_duplicate_alert_skipped_via_redis_dedup(
    set_env_budget_alert: Any,
) -> None:
    pool = _mock_pool(Decimal("90.00"))
    redis = _FakeRedis()
    redis._data["pitwall:budget-alert:2026-05:80"] = "existing_email_123"
    set_env_budget_alert(budget="100.0")
    notifier = _FakeNotifier()

    result = await check_and_send_budget_alert(
        pool,
        redis,
        now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        notifier=notifier,
    )

    assert result.skipped_duplicate is True
    assert result.email_sent is False
    assert notifier.sent == []


async def test_redis_key_has_45_day_ttl(
    set_env_budget_alert: Any,
) -> None:
    pool = _mock_pool(Decimal("85.00"))
    redis = _FakeRedis()
    set_env_budget_alert(budget="100.0")
    notifier = _FakeNotifier()

    result = await check_and_send_budget_alert(
        pool,
        redis,
        now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        notifier=notifier,
    )

    assert result.email_sent is True
    assert redis._data["pitwall:budget-alert:2026-05:80"] == "test_email_123"
    assert redis._expiry["pitwall:budget-alert:2026-05:80"] == 45 * 24 * 60 * 60


async def test_http_error_returns_error_in_result(
    set_env_budget_alert: Any,
) -> None:
    pool = _mock_pool(Decimal("85.00"))
    redis = _FakeRedis()
    set_env_budget_alert(budget="100.0")
    notifier = _FakeNotifier(NotificationResult(ok=False, error="401 Unauthorized"))

    result = await check_and_send_budget_alert(
        pool,
        redis,
        now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        notifier=notifier,
    )

    assert result.email_sent is False
    assert result.error is not None
    assert "401" in result.error


async def test_missing_env_raises() -> None:
    os.environ.pop("PITWALL_MONTHLY_BUDGET_USD", None)

    pool = _mock_pool(Decimal("85.00"))
    redis = _FakeRedis()

    with pytest.raises(ValueError, match="PITWALL_MONTHLY_BUDGET_USD"):
        await check_and_send_budget_alert(
            pool,
            redis,
            now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )
