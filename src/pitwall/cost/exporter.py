"""Pitwall cost exporter — Prometheus-style metrics endpoint.

Runs as ``python -m pitwall.cost`` on port 9109.

Exposes:
- ``pitwall_cloud_spend_month_usd`` — monthly cloud spend (USD)
- ``pitwall_cloud_budget_pct`` — percent of monthly budget consumed
- ``pitwall_active_workers`` — active lease count from DB
- ``pitwall_kill_log_triggers_7d`` — kill-switch activations in the last 7 days
- ``pitwall_providers_unhealthy`` — count of providers with health_status = 'unhealthy'
- ``pitwall_workload_queue_depth`` — queued workload count
- ``pitwall_reconciliation_lag_seconds`` — age of the oldest queued/running workload
- ``pitwall_webhook_delivery_retries_due`` — outbound retries currently due
- ``pitwall_webhook_delivery_terminal_failures_24h`` — terminal delivery failures in 24h
- ``pitwall_provider_spend_month_usd`` — monthly spend by provider
- ``pitwall_retention_last_success_timestamp_seconds`` — latest completed retention run
- ``pitwall_retention_last_deleted_count`` — rows deleted by that run

State source: Postgres ``pitwall.leases`` (active count), ``pitwall.kill_log`` (triggers), ``pitwall.providers`` (health status).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from starlette.responses import JSONResponse, Response

from pitwall.config import require_runtime_env
from pitwall.security.redaction import configure_logging_redaction

configure_logging_redaction()
require_runtime_env("cost-exporter")

log = logging.getLogger("pitwall.cost_exporter")

cloud_spend_month_usd = Gauge(
    "pitwall_cloud_spend_month_usd",
    "Cumulative monthly cloud spend (USD)",
)
cloud_budget_pct = Gauge("pitwall_cloud_budget_pct", "% of monthly budget consumed")
cloud_budget_usd = Gauge("pitwall_cloud_budget_usd", "Monthly budget (USD)")
active_workers = Gauge(
    "pitwall_active_workers",
    "Active lease count from pitwall.leases",
    ["provider"],
)
kill_log_triggers_7d = Gauge(
    "pitwall_kill_log_triggers_7d",
    "Kill-switch activations in the last 7 days",
)
providers_unhealthy = Gauge(
    "pitwall_providers_unhealthy",
    "Count of providers with health_status = 'unhealthy'",
)
workload_queue_depth = Gauge(
    "pitwall_workload_queue_depth", "Count of workloads waiting for reconciliation"
)
reconciliation_lag_seconds = Gauge(
    "pitwall_reconciliation_lag_seconds",
    "Age in seconds of the oldest queued or running workload",
)
webhook_delivery_retries_due = Gauge(
    "pitwall_webhook_delivery_retries_due",
    "Count of outbound webhook retry attempts currently due",
)
webhook_delivery_terminal_failures_24h = Gauge(
    "pitwall_webhook_delivery_terminal_failures_24h",
    "Count of terminal outbound webhook delivery failures in the last 24 hours",
)
provider_spend_month_usd = Gauge(
    "pitwall_provider_spend_month_usd",
    "Cumulative monthly workload spend by provider (USD)",
    ["provider"],
)
retention_last_success_timestamp_seconds = Gauge(
    "pitwall_retention_last_success_timestamp_seconds",
    "Unix timestamp of the latest completed retention run",
)
retention_last_deleted_count = Gauge(
    "pitwall_retention_last_deleted_count",
    "Rows deleted by the latest completed retention run",
)

BUDGET_USD = float(os.environ.get("PITWALL_MONTHLY_BUDGET_USD", "1000"))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        log.error("DATABASE_URL is not set")
        raise SystemExit(1)
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    app.state.pool = pool
    app.state.budget = BUDGET_USD
    cloud_budget_usd.set(BUDGET_USD)
    app.state._poll_task = asyncio.create_task(_poll_loop(app))
    try:
        yield
    finally:
        app.state._poll_task.cancel()
        await pool.close()


async def _poll_loop(app: FastAPI) -> None:
    while True:
        try:
            await _refresh(app)
        except Exception as exc:  # reason: poll loop must survive any refresh failure and retry
            log.exception("refresh failed: %s", exc)
        await asyncio.sleep(60)


async def _refresh(app: FastAPI) -> None:
    pool: asyncpg.Pool = app.state.pool

    async with pool.acquire() as conn:
        total_spend = await conn.fetchval(
            """SELECT COALESCE(SUM(cost_actual_usd), 0) FROM pitwall.workloads
               WHERE date_trunc('month', submitted_at AT TIME ZONE 'UTC')
                     = date_trunc('month', now() AT TIME ZONE 'UTC')
               AND state IN ('queued','running','completed')"""
        )
        active_count_rows = await conn.fetch(
            """SELECT p.name AS provider, COUNT(l.id) AS cnt
               FROM pitwall.leases l
               JOIN pitwall.providers p ON l.provider_id = p.id
               WHERE l.state = 'active'
               GROUP BY p.name"""
        )
        kills = await conn.fetchval(
            "SELECT COUNT(*) FROM pitwall.kill_log WHERE triggered_at > now() - interval '7 days'"
        )
        unhealthy_count = await conn.fetchval(
            "SELECT COUNT(*) FROM pitwall.providers WHERE health_status = 'unhealthy'"
        )
        queued_count = await conn.fetchval(
            "SELECT COUNT(*) FROM pitwall.workloads WHERE state = 'queued'"
        )
        reconciliation_lag = await conn.fetchval(
            """SELECT COALESCE(
                   EXTRACT(EPOCH FROM (now() - MIN(submitted_at))), 0
               )
               FROM pitwall.workloads
               WHERE state IN ('queued', 'running')"""
        )
        webhook_delivery = await conn.fetchrow(
            """SELECT
                 COUNT(*) FILTER (
                   WHERE next_retry_at IS NOT NULL AND next_retry_at <= now()
                 ) AS retries_due,
                 COUNT(*) FILTER (
                   WHERE next_retry_at IS NULL
                     AND attempted_at > now() - interval '24 hours'
                 ) AS terminal_failures_24h
               FROM pitwall.webhook_delivery_failures"""
        )
        provider_spend_rows = await conn.fetch(
            """SELECT p.name AS provider,
                      COALESCE(SUM(w.cost_actual_usd), 0) AS spend
               FROM pitwall.workloads w
               JOIN pitwall.providers p ON w.provider_id = p.id
               WHERE date_trunc('month', w.submitted_at AT TIME ZONE 'UTC')
                     = date_trunc('month', now() AT TIME ZONE 'UTC')
               GROUP BY p.name"""
        )
        retention_run = await conn.fetchrow(
            """SELECT EXTRACT(EPOCH FROM completed_at) AS completed_timestamp,
                      deleted_count
               FROM pitwall.retention_runs
               WHERE status = 'completed'
               ORDER BY completed_at DESC
               LIMIT 1"""
        )

    cloud_spend_month_usd.set(float(total_spend or 0))
    budget = app.state.budget
    cloud_budget_pct.set(
        float(total_spend or 0) / budget * 100.0 if budget else 0.0,
    )

    active_workers.clear()
    for row in active_count_rows:
        active_workers.labels(provider=row["provider"]).set(row["cnt"])

    kill_log_triggers_7d.set(int(kills or 0))
    providers_unhealthy.set(int(unhealthy_count or 0))
    workload_queue_depth.set(int(queued_count or 0))
    reconciliation_lag_seconds.set(float(reconciliation_lag or 0))
    webhook_delivery_retries_due.set(int(webhook_delivery["retries_due"] or 0))
    webhook_delivery_terminal_failures_24h.set(int(webhook_delivery["terminal_failures_24h"] or 0))

    provider_spend_month_usd.clear()
    for row in provider_spend_rows:
        provider_spend_month_usd.labels(provider=row["provider"]).set(float(row["spend"] or 0))

    retention_last_success_timestamp_seconds.set(
        float(retention_run["completed_timestamp"] or 0) if retention_run else 0
    )
    retention_last_deleted_count.set(
        int(retention_run["deleted_count"] or 0) if retention_run else 0
    )


app = FastAPI(lifespan=lifespan, title="Pitwall Cost Exporter", version="1")


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "service": "cost-exporter"}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "cost-exporter"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Return success only when the metrics source database is reachable."""

    pool: asyncpg.Pool | None = getattr(app.state, "pool", None)
    postgres: dict[str, Any]
    try:
        if pool is None:
            raise RuntimeError("pool unavailable")
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        postgres = {"ok": True}
    except Exception:  # pragma: no cover  # reason: dependency failures are reported, not raised
        postgres = {"ok": False, "error": "unavailable"}
    ok = bool(postgres["ok"])
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ok": ok, "postgres": postgres},
    )


def main() -> int:
    import uvicorn

    port = int(os.environ.get("PITWALL_COST_EXPORTER_PORT", "9109"))
    concurrency = int(os.environ.get("PITWALL_COST_EXPORTER_MAX_CONCURRENCY", "20"))
    if concurrency < 1:
        raise SystemExit("PITWALL_COST_EXPORTER_MAX_CONCURRENCY must be at least 1")
    uvicorn.run(app, host="0.0.0.0", port=port, limit_concurrency=concurrency)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
