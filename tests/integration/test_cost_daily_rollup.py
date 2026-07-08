"""release program Task 7: cost_daily rollup against real Postgres.

Proves run_rollup aggregates real pitwall.workloads rows into cost_daily by
UTC day, capability class, and provider type, with terminal-state filtering,
idempotent upserts, NULL-cost handling, and the success hook. Also covers the
volume_cost_daily storage accrual path.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from pitwall.reconciler.cost_daily_rollup import run_rollup
from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.anyio, pytest.mark.integration, requires_pg]


async def _insert_capability(conn, capability_id: str, name: str, class_: str) -> None:
    await conn.execute(
        """
        INSERT INTO pitwall.capabilities
            (id, name, version, class, cost_mode, config)
        VALUES ($1, $2, '1.0.0', $3, 'per_second', $4)
        """,
        capability_id,
        name,
        class_,
        {},
    )


async def _insert_provider(
    conn,
    provider_id: str,
    capability_id: str,
    provider_type: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO pitwall.providers
            (id, capability_id, name, provider_type, config, priority)
        VALUES ($1, $2, $1, $3, $4, 1)
        """,
        provider_id,
        capability_id,
        provider_type,
        {},
    )


async def _insert_workload(
    conn,
    workload_id: str,
    capability_id: str,
    provider_id: str,
    state: str,
    submitted_at: dt.datetime,
    cost_actual_usd: Decimal | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO pitwall.workloads
            (id, capability_id, provider_id, type, state, submitted_at, cost_actual_usd)
        VALUES ($1, $2, $3, 'test', $4, $5, $6)
        """,
        workload_id,
        capability_id,
        provider_id,
        state,
        submitted_at,
        cost_actual_usd,
    )


async def _seed_rollup_group(
    conn,
    capability_id: str = "cap_embedding",
    provider_id: str = "prov_queue",
    class_: str = "embedding",
    provider_type: str = "serverless_queue",
) -> None:
    await _insert_capability(conn, capability_id, f"{capability_id}.v1", class_)
    await _insert_provider(conn, provider_id, capability_id, provider_type)


async def _fetch_cost_daily(conn) -> list[tuple[dt.date, str, str, int, Decimal]]:
    rows = await conn.fetch(
        """
        SELECT day, capability_class, provider_type, workload_count, cost_usd
        FROM pitwall.cost_daily
        ORDER BY day, capability_class, provider_type
        """
    )
    return [
        (
            row["day"],
            row["capability_class"],
            row["provider_type"],
            row["workload_count"],
            row["cost_usd"],
        )
        for row in rows
    ]


async def test_aggregates_by_day_class_and_provider_type(pg_pool) -> None:
    async with pg_pool.acquire() as conn:
        await _seed_rollup_group(conn)
        await _seed_rollup_group(
            conn,
            capability_id="cap_llm",
            provider_id="prov_lb",
            class_="llm",
            provider_type="serverless_lb",
        )
        await _insert_workload(
            conn,
            "w_embedding_day1_a",
            "cap_embedding",
            "prov_queue",
            "completed",
            dt.datetime(2026, 5, 28, 10, 0, tzinfo=dt.UTC),
            Decimal("1.25"),
        )
        await _insert_workload(
            conn,
            "w_embedding_day1_b",
            "cap_embedding",
            "prov_queue",
            "completed",
            dt.datetime(2026, 5, 28, 18, 0, tzinfo=dt.UTC),
            Decimal("2.75"),
        )
        await _insert_workload(
            conn,
            "w_embedding_day2",
            "cap_embedding",
            "prov_queue",
            "completed",
            dt.datetime(2026, 5, 29, 1, 0, tzinfo=dt.UTC),
            Decimal("3.00"),
        )
        await _insert_workload(
            conn,
            "w_llm_day1",
            "cap_llm",
            "prov_lb",
            "completed",
            dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.UTC),
            Decimal("5.50"),
        )
        await _insert_workload(
            conn,
            "w_llm_day2",
            "cap_llm",
            "prov_lb",
            "completed",
            dt.datetime(2026, 5, 29, 13, 0, tzinfo=dt.UTC),
            Decimal("6.25"),
        )

    await run_rollup(pg_pool)

    async with pg_pool.acquire() as conn:
        assert await _fetch_cost_daily(conn) == [
            (dt.date(2026, 5, 28), "embedding", "serverless_queue", 2, Decimal("4.00")),
            (dt.date(2026, 5, 28), "llm", "serverless_lb", 1, Decimal("5.50")),
            (dt.date(2026, 5, 29), "embedding", "serverless_queue", 1, Decimal("3.00")),
            (dt.date(2026, 5, 29), "llm", "serverless_lb", 1, Decimal("6.25")),
        ]


async def test_terminal_only_filter_excludes_non_terminal_workloads(pg_pool) -> None:
    async with pg_pool.acquire() as conn:
        await _seed_rollup_group(conn)
        submitted_at = dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.UTC)
        for state, cost in [
            ("completed", Decimal("1.00")),
            ("failed", Decimal("2.00")),
            ("cancelled", Decimal("3.00")),
            ("timed_out", Decimal("4.00")),
            ("queued", Decimal("8.00")),
            ("running", Decimal("16.00")),
        ]:
            await _insert_workload(
                conn,
                f"w_{state}",
                "cap_embedding",
                "prov_queue",
                state,
                submitted_at,
                cost,
            )

    await run_rollup(pg_pool)

    async with pg_pool.acquire() as conn:
        assert await _fetch_cost_daily(conn) == [
            (dt.date(2026, 5, 28), "embedding", "serverless_queue", 4, Decimal("10.00"))
        ]


async def test_submitted_at_buckets_by_utc_day(pg_pool) -> None:
    offset_minus_five = dt.timezone(dt.timedelta(hours=-5))
    async with pg_pool.acquire() as conn:
        await _seed_rollup_group(conn)
        await _insert_workload(
            conn,
            "w_late_local",
            "cap_embedding",
            "prov_queue",
            "completed",
            dt.datetime(2026, 5, 28, 23, 30, tzinfo=offset_minus_five),
            Decimal("1.00"),
        )

    await run_rollup(pg_pool)

    async with pg_pool.acquire() as conn:
        assert await _fetch_cost_daily(conn) == [
            (dt.date(2026, 5, 29), "embedding", "serverless_queue", 1, Decimal("1.00"))
        ]


async def test_rollup_rerun_is_idempotent(pg_pool) -> None:
    async with pg_pool.acquire() as conn:
        await _seed_rollup_group(conn)
        await _insert_workload(
            conn,
            "w_idempotent_a",
            "cap_embedding",
            "prov_queue",
            "completed",
            dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.UTC),
            Decimal("1.50"),
        )
        await _insert_workload(
            conn,
            "w_idempotent_b",
            "cap_embedding",
            "prov_queue",
            "completed",
            dt.datetime(2026, 5, 28, 13, 0, tzinfo=dt.UTC),
            Decimal("2.50"),
        )

    await run_rollup(pg_pool)
    async with pg_pool.acquire() as conn:
        first_rows = await _fetch_cost_daily(conn)

    await run_rollup(pg_pool)
    async with pg_pool.acquire() as conn:
        second_rows = await _fetch_cost_daily(conn)

    assert first_rows == [
        (dt.date(2026, 5, 28), "embedding", "serverless_queue", 2, Decimal("4.00"))
    ]
    assert second_rows == first_rows


async def test_null_cost_actual_counts_as_zero_cost(pg_pool) -> None:
    async with pg_pool.acquire() as conn:
        await _seed_rollup_group(conn)
        await _insert_workload(
            conn,
            "w_null_cost",
            "cap_embedding",
            "prov_queue",
            "completed",
            dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.UTC),
            None,
        )

    await run_rollup(pg_pool)

    async with pg_pool.acquire() as conn:
        assert await _fetch_cost_daily(conn) == [
            (dt.date(2026, 5, 28), "embedding", "serverless_queue", 1, Decimal("0"))
        ]


async def test_after_rollup_hook_fires_once_on_success(pg_pool) -> None:
    calls = 0

    async def after_rollup() -> None:
        nonlocal calls
        calls += 1

    await run_rollup(pg_pool, after_rollup=after_rollup)

    assert calls == 1


async def test_volume_cost_daily_accrues_storage_for_null_monthly_cost(pg_pool) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pitwall.volumes
                (id, runpod_volume_id, name, datacenter_id, size_gb, monthly_cost_usd, config)
            VALUES ($1, $2, $3, $4, $5, NULL, $6)
            """,
            "vol_tier1",
            "rp_vol_tier1",
            "tier1 volume",
            "US-KS-1",
            300,
            {},
        )

    await run_rollup(pg_pool)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT day, volume_id, cost_usd, size_gb, tiered_rate_per_gb
            FROM pitwall.volume_cost_daily
            """
        )
        current_day = await conn.fetchval("SELECT CURRENT_DATE")

    assert row is not None
    assert row["day"] == current_day
    assert row["volume_id"] == "vol_tier1"
    assert row["cost_usd"] == Decimal("0.70")
    assert row["size_gb"] == 300
    assert row["tiered_rate_per_gb"] == Decimal("0.07")
