"""Real-PostgreSQL verification for operational exporter queries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import asyncpg
import pytest

from tests.integration.conftest import requires_pg

pytestmark = [pytest.mark.anyio, pytest.mark.integration, requires_pg]


async def test_refresh_exports_queue_webhook_provider_and_retention_metrics(
    pg_pool: asyncpg.Pool,
) -> None:
    now = datetime.now(UTC)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pitwall.capabilities (id,name,version,class,cost_mode,config)
            VALUES ('cap-metrics','Metrics','1','test','per_request','{}')
            """
        )
        await conn.execute(
            """
            INSERT INTO pitwall.providers
              (id,capability_id,name,provider_type,config,priority)
            VALUES ('provider-metrics','cap-metrics','Metrics provider',
                    'serverless_queue','{}',1)
            """
        )
        await conn.execute(
            """
            INSERT INTO pitwall.workloads
              (id,capability_id,provider_id,type,state,cost_actual_usd,submitted_at)
            VALUES
              ('w-metrics-done','cap-metrics','provider-metrics','test','completed',2.5,$1),
              ('w-metrics-queued','cap-metrics','provider-metrics','test','queued',NULL,$2)
            """,
            now,
            now - timedelta(minutes=10),
        )
        subscription_id = await conn.fetchval(
            """
            INSERT INTO pitwall.webhook_subscriptions (consumer,webhook_url,active)
            VALUES ('metrics','https://example.invalid/hook',false)
            RETURNING id
            """
        )
        await conn.execute(
            """
            INSERT INTO pitwall.webhook_delivery_failures
              (workload_id,subscription_id,attempt,attempted_at,next_retry_at,payload)
            VALUES
              ('w-metrics-done',$1,1,$2,$3,'{}'),
              ('w-metrics-done',$1,4,$2,NULL,'{}')
            """,
            subscription_id,
            now,
            now - timedelta(seconds=1),
        )
        await conn.execute(
            """
            INSERT INTO pitwall.retention_runs
              (id,started_at,completed_at,cutoff_at,mode,workload_count,
               deleted_count,key_version,status)
            VALUES ('ret-metrics',$1,$2,$3,'archive-purge',3,3,'v1','completed')
            """,
            now - timedelta(minutes=2),
            now - timedelta(minutes=1),
            now - timedelta(days=90),
        )

    from pitwall.cost import exporter

    app = SimpleNamespace(state=SimpleNamespace(pool=pg_pool, budget=10.0))
    await exporter._refresh(app)

    assert exporter.workload_queue_depth._value.get() == 1
    assert exporter.reconciliation_lag_seconds._value.get() >= 590
    assert exporter.webhook_delivery_retries_due._value.get() == 1
    assert exporter.webhook_delivery_terminal_failures_24h._value.get() == 1
    assert exporter.provider_spend_month_usd.labels(provider="Metrics provider")._value.get() == 2.5
    assert exporter.retention_last_success_timestamp_seconds._value.get() > 0
    assert exporter.retention_last_deleted_count._value.get() == 3
