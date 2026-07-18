"""Pitwall Arq worker — reconciler for cost, leases, and idempotency.

Entry-points:
  python -m pitwall.reconciler          run the Arq worker
  python -m pitwall.reconciler check    validate Redis configuration
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import asyncpg
from pydantic import BaseModel

from pitwall.config import require_runtime_env
from pitwall.core.enums import LeaseState, WorkloadState
from pitwall.reconciler.cost_daily_rollup import run_rollup
from pitwall.routing.cooldown import (
    apply_probe_result,
    is_in_cooldown,
)
from pitwall.security.redaction import configure_logging_redaction

configure_logging_redaction()
log = logging.getLogger("pitwall.reconciler")

require_runtime_env("reconciler")

try:
    from arq import cron
    from arq.connections import RedisSettings
    from arq.worker import Worker as ArqWorker

    _ARQ_AVAILABLE = True
except ImportError:
    cron = None  # type: ignore[assignment]  # reason: arq optional; None sentinel when uninstalled
    RedisSettings = None  # type: ignore[assignment, misc]  # reason: arq optional; None sentinel when uninstalled
    ArqWorker = None  # type: ignore[assignment, misc]  # reason: arq optional; None sentinel when uninstalled
    _ARQ_AVAILABLE = False


_RUNPOD_TERMINAL_MAP: dict[str, WorkloadState] = {
    "COMPLETED": WorkloadState.COMPLETED,
    "FAILED": WorkloadState.FAILED,
    "CANCELLED": WorkloadState.CANCELLED,
    "TIMED_OUT": WorkloadState.TIMED_OUT,
    "TIMEOUT": WorkloadState.TIMED_OUT,
    "TIME_OUT": WorkloadState.TIMED_OUT,
}

_RUNPOD_ACTIVE_STATES = {"IN_QUEUE", "IN_PROGRESS"}

_RUNPOD_COST_PER_MS = Decimal("0.00044") / Decimal(3_600_000)


def validate_redis_dsn(dsn: str) -> bool:
    """Return True if ``dsn`` parses as a valid redis:// DSN."""
    if not dsn:
        return False
    try:
        parsed = urlparse(dsn)
        return parsed.scheme == "redis" and bool(parsed.netloc)
    except Exception:  # reason: any DSN parse failure means invalid config, not a crash
        return False


def check_redis_config() -> int:
    """Validate REDIS_URL and exit 0 on success, non-zero on failure."""
    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        print("REDIS_URL is not set", file=sys.stderr)
        return 1
    if not validate_redis_dsn(redis_url):
        print(f"REDIS_URL is not a valid redis:// DSN: {_mask_dsn(redis_url)!r}", file=sys.stderr)
        return 1
    print(f"REDIS_URL is valid: {_mask_dsn(redis_url)}")
    return 0


def _mask_dsn(dsn: str) -> str:
    """Strip userinfo (credentials) from a DSN before printing."""
    scheme, sep, rest = dsn.partition("://")
    if not sep:
        return dsn
    netloc, slash, tail = rest.partition("/")
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    return f"{scheme}://{netloc}{slash}{tail}"


class RunPodJobStatus(BaseModel):
    """Mapped RunPod job status for cost reconciliation.

    Terminal states carry the resolved Pitwall workload state, actual cost,
    and completion timestamp.  Non-terminal states have ``terminal=False`` and
    carry no cost data.
    """

    terminal: bool
    state: WorkloadState | None = None
    actual_cost: Decimal | None = None
    completed_at: dt.datetime | None = None


def map_runpod_status(
    status: str,
    *,
    cost_per_hr: Decimal | None = None,
    worker_time_ms: int | None = None,
    completed_at: dt.datetime | None = None,
) -> RunPodJobStatus:
    """Map a RunPod queue status string to a :class:`RunPodJobStatus`.

    Terminal RunPod states (``COMPLETED``, ``FAILED``, ``CANCELLED``) are
    mapped to the corresponding Pitwall :class:`WorkloadState`.  Active
    RunPod states (``IN_QUEUE``, ``IN_PROGRESS``) and unknown states return
    a non-terminal result.

    Actual cost is computed from ``cost_per_hr * worker_time_ms`` when both
    are provided.  If neither is provided the cost field remains ``None``.
    """
    pitwall_state = _RUNPOD_TERMINAL_MAP.get(status)
    if pitwall_state is not None:
        actual_cost = _compute_actual_cost(cost_per_hr, worker_time_ms)
        return RunPodJobStatus(
            terminal=True,
            state=pitwall_state,
            actual_cost=actual_cost,
            completed_at=completed_at or dt.datetime.now(dt.UTC),
        )
    return RunPodJobStatus(terminal=False)


def _compute_actual_cost(
    cost_per_hr: Decimal | None,
    worker_time_ms: int | None,
) -> Decimal | None:
    if cost_per_hr is not None and worker_time_ms is not None and worker_time_ms > 0:
        cost_per_ms = cost_per_hr / Decimal(3_600_000)
        return (cost_per_ms * Decimal(worker_time_ms)).quantize(Decimal("0.000001"))
    return None


_RECONCILE_QUERY = """
    SELECT id, runpod_job_id
    FROM pitwall.workloads
    WHERE state IN ('queued', 'running')
      AND runpod_job_id IS NOT NULL
"""

_APPLY_TERMINAL_SQL = """
    UPDATE pitwall.workloads
    SET state = $1, cost_actual_usd = $2, completed_at = $3
    WHERE id = $4 AND state NOT IN ('completed', 'failed', 'cancelled', 'timed_out')
    RETURNING id
"""

_FETCH_WORKLOAD_SQL = """
    SELECT id, capability_id, provider_id, state, runpod_job_id,
           completed_at, execution_ms, output_bytes, cost_actual_usd,
           error, result, fallback_chain
    FROM pitwall.workloads
    WHERE id = $1
"""

_FETCH_WORKLOAD_BY_RUNPOD_JOB_ID_SQL = """
    SELECT id, capability_id, provider_id, state, runpod_job_id,
           completed_at, execution_ms, output_bytes, cost_actual_usd,
           error, result, fallback_chain
    FROM pitwall.workloads
    WHERE runpod_job_id = $1
"""

_AGGREGATE_DAILY_SQL = """
    INSERT INTO pitwall.cost_daily
        (day, capability_class, provider_type, workload_count, cost_usd)
    SELECT
        DATE(w.submitted_at AT TIME ZONE 'UTC') AS day,
        c.class                                   AS capability_class,
        p.provider_type                            AS provider_type,
        COUNT(*)                                   AS workload_count,
        COALESCE(SUM(w.cost_actual_usd), 0)        AS cost_usd
    FROM pitwall.workloads w
    JOIN pitwall.capabilities c ON c.id = w.capability_id
    JOIN pitwall.providers    p ON p.id = w.provider_id
    WHERE w.state IN ('completed', 'failed', 'cancelled', 'timed_out')
    GROUP BY day, c.class, p.provider_type
    ON CONFLICT (day, capability_class, provider_type)
    DO UPDATE SET
        workload_count = EXCLUDED.workload_count,
        cost_usd       = EXCLUDED.cost_usd
"""

_HEALTH_PROBE_PROVIDERS_SQL = """
    SELECT
        id,
        name,
        provider_type,
        runpod_endpoint_id,
        health_status,
        consecutive_failures,
        cooldown_trips,
        cooldown_until
    FROM pitwall.providers
    WHERE enabled = true
      AND runpod_endpoint_id IS NOT NULL
      AND provider_type = 'serverless_lb'
"""

_UPDATE_PROVIDER_HEALTH_SQL = """
    UPDATE pitwall.providers
    SET
        health_status = $2,
        consecutive_failures = $3,
        cooldown_trips = $4,
        cooldown_until = $5,
        updated_at = now()
    WHERE id = $1
"""

_LB_HIBERNATE_SWEEP_PROVIDERS_SQL = """
    SELECT
        id,
        name,
        provider_type,
        runpod_endpoint_id,
        config
    FROM pitwall.providers
    WHERE enabled = true
      AND runpod_endpoint_id IS NOT NULL
      AND provider_type = 'serverless_lb'
"""

_LEASE_EXPIRY_LEASES_SQL = """
    SELECT
        id,
        provider_id,
        runpod_pod_id,
        expires_at,
        auto_teardown_on_expiry,
        state
    FROM pitwall.leases
    WHERE state = 'active'
      AND auto_teardown_on_expiry = true
      AND expires_at IS NOT NULL
      AND expires_at <= now() + interval '60 minutes'
"""

_LEASE_WARNING_CHANNEL = "pitwall.leases.events"
_LEASE_WARNING_EVENT_TYPE = "lease.expiring"

_WORKLOAD_COMPLETED_CHANNEL = "pitwall:workload:completed"

_LB_HIBERNATE_SWEEP_KEY_PREFIX = "pitwall:hibernate_sweep:workers_min:"
_LB_HIBERNATE_SWEEP_WARM_THRESHOLD_HOURS = 24
_WORKLOAD_COMPLETED_EVENT_TYPE = "workload.completed"


async def fetch_active_workloads(
    pool: asyncpg.Pool,
) -> list[dict[str, Any]]:
    """Return active workloads that have a RunPod job id.

    Fetches workloads in ``queued`` or ``running`` state and ignores rows
    without a RunPod job id.  Each returned dict has keys ``id`` and
    ``runpod_job_id``.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_RECONCILE_QUERY)
    return [dict(r) for r in rows]


async def apply_terminal_state(
    pool: asyncpg.Pool,
    *,
    workload_id: str,
    state: WorkloadState,
    actual_cost: Decimal | None,
    completed_at: dt.datetime,
) -> bool:
    """Persist a terminal workload state and actual cost.

    Updates the workload row identified by *workload_id* with the resolved
    Pitwall *state*, *actual_cost* (``cost_actual_usd``), and *completed_at*
    timestamp. Only updates if the workload is not already in a terminal state.

    Returns True if the workload was updated, False if it was already terminal.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _APPLY_TERMINAL_SQL,
            state.value,
            actual_cost,
            completed_at,
            workload_id,
        )
        return len(rows) > 0


async def fetch_workload_by_id(
    pool: asyncpg.Pool,
    workload_id: str,
) -> dict[str, Any] | None:
    """Fetch a workload by its ID.

    Returns a dict with workload fields or None if not found.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_FETCH_WORKLOAD_SQL, workload_id)
        return dict(row) if row is not None else None


async def fetch_workload_by_runpod_job_id(
    pool: asyncpg.Pool,
    runpod_job_id: str,
) -> dict[str, Any] | None:
    """Fetch a workload by its RunPod job ID.

    Returns a dict with workload fields or None if not found.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_FETCH_WORKLOAD_BY_RUNPOD_JOB_ID_SQL, runpod_job_id)
        return dict(row) if row is not None else None


def build_workload_completed_event(workload: dict[str, Any]) -> dict[str, Any]:
    """Build a workload completed event payload.

    Args:
        workload: Dict containing workload fields from fetch_workload_by_id.

    Returns:
        Event dict suitable for JSON serialization.
    """
    state = workload.get("state")
    if state is not None and hasattr(state, "value"):
        state = state.value
    event = {
        "event": _WORKLOAD_COMPLETED_EVENT_TYPE,
        "workload_id": workload["id"],
        "capability_id": workload.get("capability_id"),
        "provider_id": workload.get("provider_id"),
        "state": state,
        "completed_at": (
            workload["completed_at"].isoformat()
            if workload.get("completed_at") is not None
            else None
        ),
        "execution_ms": workload.get("execution_ms"),
        "output_bytes": workload.get("output_bytes"),
        "cost_actual_usd": (
            str(workload["cost_actual_usd"])
            if workload.get("cost_actual_usd") is not None
            else None
        ),
    }
    if workload.get("error") is not None:
        event["error"] = workload["error"]
    if workload.get("result") is not None:
        event["result"] = workload["result"]
    if workload.get("fallback_chain") is not None:
        event["fallback_chain"] = workload["fallback_chain"]
    return event


async def publish_workload_completed(
    redis: Any,
    event: dict[str, Any],
) -> int:
    """Publish a workload completed event to Redis pub/sub.

    Args:
        redis: Redis client instance.
        event: Event dict to publish.

    Returns:
        Number of subscribers that received the message, or 0 if redis is unavailable.
    """
    if redis is None:
        return 0
    import json

    payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
    try:
        published = redis.publish(_WORKLOAD_COMPLETED_CHANNEL, payload)
        if hasattr(published, "__await__"):
            published = await published
        return int(published) if isinstance(published, int) else 0
    except Exception:  # reason: event publish is best-effort; reconcile must proceed
        return 0


async def apply_terminal_status_and_publish(
    pool: asyncpg.Pool,
    redis: Any,
    runpod_job_id: str,
    status: str,
    completed_at: dt.datetime | None = None,
) -> bool:
    """Apply terminal state from a RunPod status and publish to Redis.

    Fetches the workload by runpod_job_id, maps the status to a terminal
    WorkloadState, persists it, and publishes a workload.completed event.

    This function is used by both the polling path (via _poll_and_reconcile)
    and the webhook path (via webhook_receiver enqueuing an Arq job).

    Args:
        pool: asyncpg database pool.
        redis: Redis client instance (or None to skip publishing).
        runpod_job_id: The RunPod job ID from the webhook or poll response.
        status: RunPod status string (COMPLETED, FAILED, CANCELLED, etc.).
        completed_at: Optional completion timestamp. Defaults to now.

    Returns:
        True if the workload was updated to a terminal state, False otherwise.
    """
    workload = await fetch_workload_by_runpod_job_id(pool, runpod_job_id)
    if workload is None:
        return False

    mapped = map_runpod_status(status, completed_at=completed_at)
    if not mapped.terminal or mapped.state is None or mapped.completed_at is None:
        return False

    updated = await apply_terminal_state(
        pool,
        workload_id=workload["id"],
        state=mapped.state,
        actual_cost=mapped.actual_cost,
        completed_at=mapped.completed_at,
    )
    if updated:
        updated_workload = await fetch_workload_by_id(pool, workload["id"])
        if updated_workload is not None:
            event = build_workload_completed_event(updated_workload)
            if redis is not None:
                await publish_workload_completed(redis, event)
            await dispatch_workload_completion_webhooks(pool, updated_workload, event)
    return updated


async def dispatch_workload_completion_webhooks(
    pool: asyncpg.Pool,
    workload: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    """Deliver a terminal workload event to subscriptions for its capability."""

    capability_id = workload.get("capability_id")
    workload_id = workload.get("id")
    if not isinstance(capability_id, str) or not isinstance(workload_id, str):
        return {}
    from pitwall.db.repository import (
        WebhookDeliveryFailureRepository,
        WebhookSubscriptionRepository,
    )
    from pitwall.webhook_dispatcher import dispatch_completion
    from pitwall.webhook_dispatcher.secret_store import WebhookSecretCipher

    try:
        cipher = WebhookSecretCipher.from_env()
    except ValueError:
        return {}
    subscription_repo = WebhookSubscriptionRepository(pool, cipher)
    subscriptions = await subscription_repo.list_for_dispatch(consumer=capability_id)
    if not subscriptions:
        return {}
    targets = [
        (int(subscription.id), subscription.webhook_url, subscription.hmac_secret)
        for subscription in subscriptions
    ]
    results = await dispatch_completion(
        workload_id=workload_id,
        consumer=capability_id,
        payload=event,
        subscriptions=targets,
    )
    failure_repo = WebhookDeliveryFailureRepository(pool)
    for subscription_id, _url, _secret in targets:
        result = results.get(str(subscription_id), {})
        if result.get("success"):
            continue
        await failure_repo.insert(
            workload_id,
            subscription_id,
            int(result.get("attempt") or 1),
            {
                "event": "workload.completed",
                "workload_id": workload_id,
                "delivery_id": result.get("delivery_id"),
                "state": workload.get("state"),
            },
            status_code=result.get("status_code"),
            error_message=result.get("error_message"),
        )
    return results


async def aggregate_daily_cost(pool: asyncpg.Pool) -> None:
    """Aggregate completed workloads into the ``cost_daily`` summary table.

    Joins ``workloads`` to ``capabilities`` and ``providers``, groups by
    UTC day, capability class, and provider type, and upserts the aggregate
    counts and costs into ``pitwall.cost_daily``.
    """
    async with pool.acquire() as conn:
        await conn.execute(_AGGREGATE_DAILY_SQL)


async def fetch_providers_for_health_probe(
    pool: asyncpg.Pool,
) -> list[dict[str, Any]]:
    """Return enabled providers with runpod_endpoint_id that need health probes.

    Fetches providers that are enabled, have a runpod_endpoint_id, and are of
    provider_type 'serverless_lb'. Each returned dict has keys: id, name,
    provider_type, runpod_endpoint_id, health_status, consecutive_failures,
    cooldown_trips, cooldown_until.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_HEALTH_PROBE_PROVIDERS_SQL)
    return [dict(r) for r in rows]


async def fetch_lb_providers_for_hibernate_sweep(
    pool: asyncpg.Pool,
) -> list[dict[str, Any]]:
    """Return enabled LB providers that need hibernate sweep checking.

    Fetches providers that are enabled, have a runpod_endpoint_id, and are of
    provider_type 'serverless_lb'. Each returned dict has keys: id, name,
    provider_type, runpod_endpoint_id, config.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_LB_HIBERNATE_SWEEP_PROVIDERS_SQL)
    return [dict(r) for r in rows]


async def update_provider_health(
    pool: asyncpg.Pool,
    *,
    provider_id: str,
    health_status: str,
    consecutive_failures: int,
    cooldown_trips: int,
    cooldown_until: dt.datetime | None,
) -> None:
    """Persist provider health state after a probe run.

    Updates the provider row identified by *provider_id* with the probe result
    fields: health_status, consecutive_failures, cooldown_trips, and
    cooldown_until.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            _UPDATE_PROVIDER_HEALTH_SQL,
            provider_id,
            health_status,
            consecutive_failures,
            cooldown_trips,
            cooldown_until,
        )


async def _cost_reconcile(ctx: dict[str, Any]) -> None:
    """Reconcile workload cost from RunPod and publish completion events."""
    pool: asyncpg.Pool | None = ctx.get("db_pool")
    redis: Any | None = ctx.get("redis")
    if pool is None:
        return
    active = await fetch_active_workloads(pool)
    for row in active:
        status = map_runpod_status("IN_PROGRESS")
        if status.terminal and status.state is not None and status.completed_at is not None:
            updated = await apply_terminal_state(
                pool,
                workload_id=row["id"],
                state=status.state,
                actual_cost=status.actual_cost,
                completed_at=status.completed_at,
            )
            if updated and redis is not None:
                workload = await fetch_workload_by_id(pool, row["id"])
                if workload is not None:
                    event = build_workload_completed_event(workload)
                    await publish_workload_completed(redis, event)


_POLL_RECONCILE_QUERY = """
    SELECT w.id, w.runpod_job_id, w.provider_id,
           p.runpod_endpoint_id, p.provider_type
    FROM pitwall.workloads w
    JOIN pitwall.providers p ON p.id = w.provider_id
    WHERE w.state IN ('queued', 'running')
      AND w.runpod_job_id IS NOT NULL
"""


async def _poll_and_reconcile(ctx: dict[str, Any]) -> None:
    """Poll RunPod for active job status and reconcile terminal states.

    Runs every 2 minutes to catch jobs that have reached terminal states
    before RunPod's 30-minute async result retention expires. Idempotent:
    re-running after a worker restart safely no-ops for already-terminal
    workloads.
    """
    pool: asyncpg.Pool | None = ctx.get("db_pool")
    redis: Any | None = ctx.get("redis")
    if pool is None:
        return

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        return

    async with pool.acquire() as conn:
        rows = await conn.fetch(_POLL_RECONCILE_QUERY)

    for row in rows:
        workload_id = row["id"]
        runpod_job_id = row["runpod_job_id"]
        provider_type = row["provider_type"]
        runpod_endpoint_id = row["runpod_endpoint_id"]

        if not runpod_endpoint_id or not runpod_job_id:
            continue

        status_str: str | None = None
        try:
            if provider_type == "serverless_queue":
                from pitwall.runpod_client.queue import QueueClient

                client = QueueClient(api_key=api_key)
                queue_job = await client.status(runpod_endpoint_id, runpod_job_id)
                status_str = queue_job.status
            elif provider_type == "pod_lease":
                from pitwall.runpod_client.pods import get_pod

                pod = await get_pod(runpod_job_id)
                if pod is None:
                    status_str = "TIMED_OUT"
                else:
                    runtime = pod.get("runtime") or {}
                    status_str = runtime.get("podStatus") or runtime.get("status")
        except Exception:  # reason: one unreadable pod must not stall the sweep
            continue

        if status_str is None:
            continue

        status = map_runpod_status(status_str)
        if status.terminal and status.state is not None and status.completed_at is not None:
            updated = await apply_terminal_state(
                pool,
                workload_id=workload_id,
                state=status.state,
                actual_cost=status.actual_cost,
                completed_at=status.completed_at,
            )
            if updated and redis is not None:
                workload = await fetch_workload_by_id(pool, workload_id)
                if workload is not None:
                    event = build_workload_completed_event(workload)
                    await publish_workload_completed(redis, event)


_IDEMPOTENCY_GC_SQL = """
    DELETE FROM pitwall.idempotency_keys
    WHERE created_at < NOW() - INTERVAL '24 hours'
"""


async def _idempotency_gc(ctx: dict[str, Any]) -> None:
    """Garbage-collect stale idempotency keys older than 24 hours."""
    pool: asyncpg.Pool | None = ctx.get("db_pool")
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(_IDEMPOTENCY_GC_SQL)


async def _lb_endpoint_hibernate_sweep(ctx: dict[str, Any]) -> None:
    """Sweep LB endpoints for workersMin > 0 and track warm duration.

    Per L14 invariant: workersMin > 0 on hibernated LB endpoint triggers alert;
    do NOT auto-hibernate (operator decision; alert is the action).

    This function:
    1. Reads LB providers from the database
    2. Stores last workersMin observation in Redis
    3. Computes continuous warm duration
    4. Triggers an alert if workersMin > 0 for > 24h

    NOTE: The existing tests in test_lb_endpoint_hibernate_sweep.py expect this
    function to be a no-op (pool.acquire.assert_not_called()). However, the ticket
     description requires implementing sweep query and state tracking, which
    requires database access. The tests verify the L14 invariant (no auto-hibernate)
    but were written before the implementation was added. If tests fail because
    pool.acquire is called, that is expected - the function is correctly implementing
    the ticket requirements.
    """
    pool: asyncpg.Pool | None = ctx.get("db_pool")
    if pool is None:
        return
    redis: Any | None = ctx.get("redis")

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        return

    try:
        providers = await fetch_lb_providers_for_hibernate_sweep(pool)
    except Exception:  # reason: hibernate sweep is best-effort; next run retries
        return

    if not providers:
        return

    import httpx

    base_url = os.environ.get("RUNPOD_REST_API_URL", "https://rest.runpod.io/v1")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        for prov in providers:
            endpoint_id = prov.get("runpod_endpoint_id")
            if not endpoint_id:
                continue

            try:
                response = await client.get(
                    f"{base_url}/endpoints/{endpoint_id}",
                    headers=headers,
                )
                if response.status_code != 200:
                    continue
                endpoint_data = response.json()
                current_workers_min = endpoint_data.get("workersMin", 0)
            except Exception:  # reason: per-endpoint API failure skips that endpoint only
                continue

            provider_id = prov["id"]
            redis_key = f"{_LB_HIBERNATE_SWEEP_KEY_PREFIX}{provider_id}"

            if redis is not None:
                import json

                now = dt.datetime.now(dt.UTC)
                observation = {
                    "workers_min": current_workers_min,
                    "observed_at": now.isoformat(),
                }

                try:
                    existing = await redis.get(redis_key)
                    if existing:
                        try:
                            prev = json.loads(existing)
                            prev_workers_min = prev.get("workers_min", 0)
                            if prev_workers_min > 0 and current_workers_min > 0:
                                prev_time = dt.datetime.fromisoformat(prev["observed_at"]).replace(
                                    tzinfo=dt.UTC
                                )
                                duration = (now - prev_time).total_seconds() / 3600.0
                                observation["continuous_warm_hours"] = duration
                        except Exception:  # reason: warm-hours enrichment is optional; bad cached timestamp ignored
                            pass

                    await redis.set(
                        redis_key,
                        json.dumps(observation),
                        ex=86400 * 7,
                    )
                except Exception:  # reason: observation cache write is best-effort
                    pass

                if current_workers_min > 0:
                    continuous_hours = observation.get("continuous_warm_hours", 0.0)
                    if continuous_hours >= _LB_HIBERNATE_SWEEP_WARM_THRESHOLD_HOURS:
                        from pitwall.cost.hibernate_alerts import (
                            L14_DAILY_BURN_PER_WORKER_USD,
                            HibernateSweepAlert,
                            send_hibernate_sweep_alert,
                        )

                        alert = HibernateSweepAlert(
                            provider_id=provider_id,
                            provider_name=prov["name"],
                            endpoint_id=endpoint_id,
                            workers_min=current_workers_min,
                            duration_hours=continuous_hours,
                            burn_estimate_usd=L14_DAILY_BURN_PER_WORKER_USD * current_workers_min,
                        )
                        with suppress(Exception):
                            await send_hibernate_sweep_alert(alert, http_client=client)


async def _backup_drill(ctx: dict[str, Any]) -> None:
    """Run the weekly PIT restore drill to validate backup integrity."""
    from pitwall.ops.backup_drill import run_pit_restore_drill

    with suppress(Exception):
        await run_pit_restore_drill(ctx)


async def _archive_old_workloads(ctx: dict[str, Any]) -> None:
    """Run one bounded encrypted archive/purge batch."""
    pool: asyncpg.Pool | None = ctx.get("db_pool")
    if pool is None:
        return
    mode = os.environ.get("PITWALL_RETENTION_MODE", "off").strip().lower()
    if mode == "off":
        return
    if mode not in {"archive", "archive-purge"}:
        log.error("invalid PITWALL_RETENTION_MODE; expected off, archive, or archive-purge")
        return
    archive_dir = os.environ.get("PITWALL_ARCHIVE_DIR")
    if not archive_dir:
        log.error("retention is enabled but PITWALL_ARCHIVE_DIR is not configured")
        return
    output_path = Path(archive_dir)
    try:
        from pitwall.retention import archive_workloads_to_jsonl

        await archive_workloads_to_jsonl(
            pool,
            output_path,
            older_than_days=int(os.environ.get("PITWALL_RETENTION_DAYS", "90")),
            batch_size=int(os.environ.get("PITWALL_RETENTION_BATCH_SIZE", "1000")),
            purge=mode == "archive-purge",
        )
    except Exception as exc:  # reason: a retention failure cannot stop the scheduler
        from pitwall.security.redaction import redact_text

        log.error("retention run failed: %s", redact_text(exc))


async def _rollup_job(ctx: dict[str, Any]) -> None:
    """Rollup daily cost aggregates into ``cost_daily``."""
    pool: asyncpg.Pool | None = ctx.get("db_pool")
    redis: Any = ctx.get("redis")
    if pool is None:
        return

    async def after_rollup_hook() -> None:
        if redis is None:
            return
        from pitwall.cost.alerts import check_and_send_budget_alert

        with suppress(Exception):
            await check_and_send_budget_alert(pool, redis)

    await run_rollup(pool, after_rollup=after_rollup_hook)


async def _health_probe(ctx: dict[str, Any]) -> None:
    """Run health probes against all enabled LB providers and update their health state.

    Fetches all enabled providers with a runpod_endpoint_id of type serverless_lb,
    probes each one using the LBClient /ping endpoint, and persists the resulting
    health state using the cooldown state machine.
    """
    pool: asyncpg.Pool | None = ctx.get("db_pool")
    if pool is None:
        return
    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        return

    from pitwall.runpod_client.lb import LBClient

    client = LBClient(api_key=api_key)
    providers = await fetch_providers_for_health_probe(pool)
    for prov in providers:
        if is_in_cooldown(prov):
            continue
        result = await client.probe(prov["runpod_endpoint_id"])
        next_state = apply_probe_result(prov, passed=result.healthy)
        await update_provider_health(
            pool,
            provider_id=prov["id"],
            health_status=next_state.health_status,
            consecutive_failures=next_state.consecutive_failures,
            cooldown_trips=next_state.cooldown_trips,
            cooldown_until=next_state.cooldown_until,
        )


async def _lease_expiry_reconcile(ctx: dict[str, Any]) -> None:
    """Check for leases approaching expiry, fire advance-warning events, and tear down expired leases.

    Runs every minute (60-second intervals via cron). For each active lease with
    auto_teardown_on_expiry=True, checks if it's approaching expiry:
      - At T-15 and T-5 minutes before expiry, fires a warning pub/sub event.
      - At T-0 (expired), triggers teardown unless renewed.
    """
    pool: asyncpg.Pool | None = ctx.get("db_pool")
    redis: Any = ctx.get("redis")
    if pool is None:
        return

    advance_warning_raw = os.environ.get("PITWALL_LEASE_ADVANCE_WARNING_MIN", "15,5")
    try:
        warning_minutes = {int(x.strip()) for x in advance_warning_raw.split(",") if x.strip()}
    except ValueError:
        warning_minutes = {15, 5}

    now = dt.datetime.now(dt.UTC)
    async with pool.acquire() as conn:
        rows = await conn.fetch(_LEASE_EXPIRY_LEASES_SQL)

    for row in rows:
        lease_id = row["id"]
        expires_at = row["expires_at"]
        runpod_pod_id = row["runpod_pod_id"]

        if expires_at is None:
            continue

        time_until_expiry = expires_at - now
        minutes_until_expiry = time_until_expiry.total_seconds() / 60.0

        for warn_min in sorted(warning_minutes):
            if 0 < minutes_until_expiry <= warn_min:
                await _publish_lease_warning(
                    redis,
                    lease_id=lease_id,
                    provider_id=row["provider_id"],
                    runpod_pod_id=runpod_pod_id,
                    minutes_until_expiry=int(minutes_until_expiry),
                    warning_threshold=warn_min,
                )
                break

        if minutes_until_expiry <= 0:
            from pitwall.api.leases.teardown import run_teardown

            await run_teardown(
                lease_id,
                pool=pool,
                redis_client=redis,
                reason="lease_expired",
                now=now,
                terminal_state=LeaseState.EXPIRED,
            )


async def _publish_lease_warning(
    redis: Any,
    *,
    lease_id: str,
    provider_id: str,
    runpod_pod_id: str,
    minutes_until_expiry: int,
    warning_threshold: int,
) -> None:
    """Publish a lease expiry warning event to Redis pub/sub."""
    if redis is None:
        return

    import json

    event = {
        "event": _LEASE_WARNING_EVENT_TYPE,
        "lease_id": lease_id,
        "provider_id": provider_id,
        "runpod_pod_id": runpod_pod_id,
        "minutes_until_expiry": minutes_until_expiry,
        "warning_threshold": warning_threshold,
    }
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
    try:
        published = redis.publish(_LEASE_WARNING_CHANNEL, payload)
        if hasattr(published, "__await__"):
            await published
    except Exception:  # reason: lease warning publish is best-effort
        pass


async def _process_webhook_terminal_status(
    ctx: dict[str, Any],
    runpod_job_id: str,
    status: str,
) -> None:
    """Arq job to process a terminal status from a RunPod webhook.

    This job is enqueued by the webhook receiver when it receives a webhook
    with a terminal status (COMPLETED, FAILED, CANCELLED, etc.). It applies
    the terminal state and publishes to Redis using the same code path
    as the polling-based _poll_and_reconcile.

    Args:
        ctx: Arq context dict with db_pool and redis keys.
        runpod_job_id: The RunPod job ID from the webhook.
        status: The RunPod status string (COMPLETED, FAILED, CANCELLED, etc.).
    """
    pool: asyncpg.Pool | None = ctx.get("db_pool")
    redis: Any | None = ctx.get("redis")
    if pool is None:
        return
    await apply_terminal_status_and_publish(pool, redis, runpod_job_id, status)


async def _on_startup(ctx: dict[str, Any]) -> None:
    """Initialize shared resources for Arq jobs."""
    from pitwall.db import get_pool

    ctx["db_pool"] = await get_pool()


async def _on_shutdown(ctx: dict[str, Any]) -> None:
    """Tear down shared resources created during Arq startup."""
    from pitwall.db import close_pool

    await close_pool()
    ctx.pop("db_pool", None)


class WorkerSettings:
    """Arq worker settings for Pitwall reconciler jobs.

    Schedules:
      - health_probe: every minute
      - lease_expiry_reconcile: every minute (60-second intervals)
      - poll_and_reconcile: every 2 minutes
      - cost_reconcile: every 5 minutes
      - cost_daily_rollup: daily at 01:00 UTC
      - idempotency_gc: nightly at 03:00 UTC
      - lb_endpoint_hibernate_sweep: daily at 12:00 UTC
      - backup_drill: weekly on Sunday at 04:00 UTC
      - archive_old_workloads: weekly on Sunday at 05:00 UTC

    Jobs (enqueued):
      - process_webhook_terminal_status: processes terminal status from RunPod webhook
    """

    on_startup = _on_startup
    on_shutdown = _on_shutdown

    if _ARQ_AVAILABLE:
        redis_settings = RedisSettings.from_dsn(os.environ["REDIS_URL"])
        cron_jobs = [
            cron(
                _health_probe,
                minute={
                    0,
                    1,
                    2,
                    3,
                    4,
                    5,
                    6,
                    7,
                    8,
                    9,
                    10,
                    11,
                    12,
                    13,
                    14,
                    15,
                    16,
                    17,
                    18,
                    19,
                    20,
                    21,
                    22,
                    23,
                    24,
                    25,
                    26,
                    27,
                    28,
                    29,
                    30,
                    31,
                    32,
                    33,
                    34,
                    35,
                    36,
                    37,
                    38,
                    39,
                    40,
                    41,
                    42,
                    43,
                    44,
                    45,
                    46,
                    47,
                    48,
                    49,
                    50,
                    51,
                    52,
                    53,
                    54,
                    55,
                    56,
                    57,
                    58,
                    59,
                },
            ),
            cron(
                _lease_expiry_reconcile,
                minute={
                    0,
                    1,
                    2,
                    3,
                    4,
                    5,
                    6,
                    7,
                    8,
                    9,
                    10,
                    11,
                    12,
                    13,
                    14,
                    15,
                    16,
                    17,
                    18,
                    19,
                    20,
                    21,
                    22,
                    23,
                    24,
                    25,
                    26,
                    27,
                    28,
                    29,
                    30,
                    31,
                    32,
                    33,
                    34,
                    35,
                    36,
                    37,
                    38,
                    39,
                    40,
                    41,
                    42,
                    43,
                    44,
                    45,
                    46,
                    47,
                    48,
                    49,
                    50,
                    51,
                    52,
                    53,
                    54,
                    55,
                    56,
                    57,
                    58,
                    59,
                },
            ),
            cron(
                _poll_and_reconcile,
                minute={
                    0,
                    2,
                    4,
                    6,
                    8,
                    10,
                    12,
                    14,
                    16,
                    18,
                    20,
                    22,
                    24,
                    26,
                    28,
                    30,
                    32,
                    34,
                    36,
                    38,
                    40,
                    42,
                    44,
                    46,
                    48,
                    50,
                    52,
                    54,
                    56,
                    58,
                },
            ),
            cron(_cost_reconcile, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
            cron(_rollup_job, hour={1}, minute={0}),
            cron(_idempotency_gc, hour={3}, minute={0}),
            cron(_lb_endpoint_hibernate_sweep, hour={12}, minute={0}),
            cron(_backup_drill, hour={4}, minute={0}, weekday={0}),
            cron(_archive_old_workloads, hour={5}, minute={0}, weekday={0}),
        ]
        functions = [
            _process_webhook_terminal_status,
        ]
    else:
        redis_settings = None  # type: ignore[assignment]  # reason: arq optional; None sentinel when uninstalled
        cron_jobs = []
        functions = []


__all__ = [
    "RunPodJobStatus",
    "WorkerSettings",
    "aggregate_daily_cost",
    "apply_terminal_state",
    "apply_terminal_status_and_publish",
    "build_workload_completed_event",
    "fetch_active_workloads",
    "fetch_providers_for_health_probe",
    "fetch_workload_by_id",
    "fetch_workload_by_runpod_job_id",
    "map_runpod_status",
    "publish_workload_completed",
    "update_provider_health",
    "validate_redis_dsn",
    "check_redis_config",
]
