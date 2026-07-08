from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.cost.threshold_alerts import (
    DEFAULT_THRESHOLDS,
    ThresholdCrossing,
    evaluate_crossings,
    record_crossings,
    send_crossing_notifications,
)


def test_evaluate_crossings_returns_empty_when_no_thresholds_crossed() -> None:
    pool = _mock_pool(spend=Decimal("100.00"), alert_events=[])
    crossings = asyncio.run(
        evaluate_crossings(
            pool,
            budget_usd=1000.0,
            thresholds=(50, 75, 90),
            now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )
    )
    assert crossings == []


def test_evaluate_crossings_returns_50_pct_when_crossed() -> None:
    pool = _mock_pool(spend=Decimal("550.00"), alert_events=[])
    crossings = asyncio.run(
        evaluate_crossings(
            pool,
            budget_usd=1000.0,
            thresholds=(50, 75, 90),
            now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )
    )
    assert len(crossings) == 1
    assert crossings[0].threshold_pct == 50
    assert crossings[0].spend_usd == 550.0
    assert crossings[0].budget_usd == 1000.0
    assert crossings[0].budget_pct == pytest.approx(55.0)


def test_evaluate_crossings_returns_multiple_crossings() -> None:
    pool = _mock_pool(spend=Decimal("950.00"), alert_events=[])
    crossings = asyncio.run(
        evaluate_crossings(
            pool,
            budget_usd=1000.0,
            thresholds=(50, 75, 90),
            now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )
    )
    assert len(crossings) == 3
    threshold_pcts = {c.threshold_pct for c in crossings}
    assert threshold_pcts == {50, 75, 90}


def test_evaluate_crossings_skips_already_recorded_thresholds() -> None:
    pool = _mock_pool(spend=Decimal("950.00"), alert_events=[(50,), (75,)])
    crossings = asyncio.run(
        evaluate_crossings(
            pool,
            budget_usd=1000.0,
            thresholds=(50, 75, 90),
            now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )
    )
    assert len(crossings) == 1
    assert crossings[0].threshold_pct == 90


def test_evaluate_crossings_skips_all_already_recorded() -> None:
    pool = _mock_pool(spend=Decimal("950.00"), alert_events=[(50,), (75,), (90,)])
    crossings = asyncio.run(
        evaluate_crossings(
            pool,
            budget_usd=1000.0,
            thresholds=(50, 75, 90),
            now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )
    )
    assert crossings == []


def test_evaluate_crossings_uses_default_thresholds() -> None:
    pool = _mock_pool(spend=Decimal("500.00"), alert_events=[])
    crossings = asyncio.run(
        evaluate_crossings(
            pool,
            budget_usd=1000.0,
            now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )
    )
    threshold_pcts = {c.threshold_pct for c in crossings}
    assert threshold_pcts == {50}


def test_evaluate_crossings_returns_empty_for_empty_thresholds() -> None:
    pool = _mock_pool(spend=Decimal("500.00"), alert_events=[])
    crossings = asyncio.run(
        evaluate_crossings(
            pool,
            budget_usd=1000.0,
            thresholds=(),
            now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )
    )
    assert crossings == []


def test_evaluate_crossings_budget_pct_calculation() -> None:
    pool = _mock_pool(spend=Decimal("250.00"), alert_events=[])
    crossings = asyncio.run(
        evaluate_crossings(
            pool,
            budget_usd=1000.0,
            thresholds=(25,),
            now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )
    )
    assert len(crossings) == 1
    assert crossings[0].budget_pct == 25.0


def test_evaluate_crossings_zero_budget_returns_empty() -> None:
    pool = _mock_pool(spend=Decimal("100.00"), alert_events=[])
    crossings = asyncio.run(
        evaluate_crossings(
            pool,
            budget_usd=0.0,
            thresholds=(50, 75, 90),
            now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )
    )
    assert crossings == []


def test_evaluate_crossings_zero_budget_does_not_cross_low_threshold() -> None:
    pool = _mock_pool(spend=Decimal("100.00"), alert_events=[])
    crossings = asyncio.run(
        evaluate_crossings(
            pool,
            budget_usd=0.0,
            thresholds=(1,),
            now=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )
    )
    assert crossings == []


def test_evaluate_crossings_uses_provided_month_for_database_queries() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    pool = _mock_pool(spend=Decimal("550.00"), alert_events=[])

    crossings = asyncio.run(
        evaluate_crossings(
            pool,
            budget_usd=1000.0,
            thresholds=(50,),
            now=now,
        )
    )

    assert [crossing.threshold_pct for crossing in crossings] == [50]
    fetchrow_args = pool.conn.fetchrow.await_args.args
    assert "SUM(cost_actual_usd)" in fetchrow_args[0]
    assert fetchrow_args[1] == now
    fetch_args = pool.conn.fetch.await_args.args
    assert "pitwall.alert_events" in fetch_args[0]
    assert fetch_args[1] == "2026-05"


def test_evaluate_crossings_default_now_uses_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    seen_timezones: list[object] = []

    class FixedDateTime:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            seen_timezones.append(tz)
            return fixed_now

    monkeypatch.setattr("pitwall.cost.threshold_alerts.datetime", FixedDateTime)
    pool = _mock_pool(spend=Decimal("550.00"), alert_events=[])

    crossings = asyncio.run(evaluate_crossings(pool, budget_usd=1000.0, thresholds=(50,)))

    assert [crossing.threshold_pct for crossing in crossings] == [50]
    assert seen_timezones == [UTC]
    assert pool.conn.fetchrow.await_args.args[1] == fixed_now
    assert pool.conn.fetch.await_args.args[1] == "2026-05"


def test_record_crossings_inserts_alert_events() -> None:
    pool = _mock_pool_for_record(spend=Decimal("0"))
    now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    crossings = [
        ThresholdCrossing(
            threshold_pct=50,
            spend_usd=550.0,
            budget_usd=1000.0,
            budget_pct=55.0,
        ),
        ThresholdCrossing(
            threshold_pct=75,
            spend_usd=550.0,
            budget_usd=1000.0,
            budget_pct=55.0,
        ),
    ]
    asyncio.run(
        record_crossings(
            pool,
            crossings,
            now=now,
        )
    )
    assert pool.conn.execute.await_count == 2
    first_args = pool.conn.execute.await_args_list[0].args
    second_args = pool.conn.execute.await_args_list[1].args
    assert "INSERT INTO pitwall.alert_events" in first_args[0]
    assert "ON CONFLICT (month, threshold_pct) DO NOTHING" in first_args[0]
    assert first_args[1:] == ("2026-05", 50, now)
    assert second_args[1:] == ("2026-05", 75, now)


def test_record_crossings_default_now_uses_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    seen_timezones: list[object] = []

    class FixedDateTime:
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            seen_timezones.append(tz)
            return fixed_now

    monkeypatch.setattr("pitwall.cost.threshold_alerts.datetime", FixedDateTime)
    pool = _mock_pool_for_record(spend=Decimal("0"))
    crossing = ThresholdCrossing(
        threshold_pct=50,
        spend_usd=550.0,
        budget_usd=1000.0,
        budget_pct=55.0,
    )

    asyncio.run(record_crossings(pool, [crossing]))

    assert seen_timezones == [UTC]
    assert pool.conn.execute.await_args.args[1:] == ("2026-05", 50, fixed_now)


def test_record_crossings_empty_list_does_nothing() -> None:
    pool = _mock_pool_for_record(spend=Decimal("0"))
    asyncio.run(record_crossings(pool, []))
    pool.conn.execute.assert_not_awaited()


def test_default_thresholds_are_50_75_90() -> None:
    assert DEFAULT_THRESHOLDS == (50, 75, 90)


def test_send_crossing_notifications_dispatches_each_crossing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pitwall.cost.notifications import NotificationResult

    crossings = [
        ThresholdCrossing(50, spend_usd=550.0, budget_usd=1000.0, budget_pct=55.0),
        ThresholdCrossing(75, spend_usd=800.0, budget_usd=1000.0, budget_pct=80.0),
    ]
    sent_crossings: list[ThresholdCrossing] = []

    def fake_send_threshold_email(crossing: ThresholdCrossing) -> NotificationResult:
        sent_crossings.append(crossing)
        return NotificationResult(
            threshold_pct=crossing.threshold_pct,
            email_id=f"email-{crossing.threshold_pct}",
        )

    monkeypatch.setattr(
        "pitwall.cost.notifications.send_threshold_email",
        fake_send_threshold_email,
    )

    results = asyncio.run(send_crossing_notifications(crossings))

    assert sent_crossings == crossings
    assert [(result.threshold_pct, result.email_id) for result in results] == [
        (50, "email-50"),
        (75, "email-75"),
    ]


def _mock_pool(spend: Decimal, alert_events: list[tuple[int, ...]]) -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()

    async def mock_fetchrow(sql: str, *args: object) -> dict[str, object]:
        if "SUM(cost_actual_usd)" in sql:
            return {"total": spend}
        return {}

    async def mock_fetch(sql: str, *args: object) -> list[dict[str, int]]:
        if "alert_events" in sql:
            return [{"threshold_pct": row[0]} for row in alert_events]
        return []

    conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)
    conn.fetch = AsyncMock(side_effect=mock_fetch)

    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acq)
    pool.conn = conn
    return pool


def _mock_pool_for_record(spend: Decimal) -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acq)
    pool.conn = conn
    return pool
