# Reconciler & Workload Lifecycle

## 1. Purpose & Scope

The Reconciler & Workload Lifecycle subsystem is the operational heart of Pitwall's background processing. It:

- **Tracks workload state** from queued through terminal outcomes (completed, failed, cancelled, timed_out) via RunPod polling and webhook ingestion.
- **Reconciles cost** by computing actual spend from RunPod runtime and writing it back to workload rows and the `cost_daily` summary table.
- **Manages lease lifecycle** by emitting advance-warning events and tearing down expired leases.
- **Monitors provider health** via periodic LB probe runs whose results feed the cooldown state machine.
- **Drives daily cost rollup**, idempotency GC, hibernate-sweep alerting, backup drills, and workload archiving — all scheduled via Arq cron.
- **Fails closed for the removed legacy GPU worker CLI** (`pitwall.worker`); the only supported background worker is the reconciler Arq process.

The subsystem sits between the API layer (which creates workloads) and the RunPod API (which executes them). It is the authoritative source of truth for workload terminal states and actual cost.

---

## 2. Components

### `src/pitwall/reconciler/__init__.py` — Main Reconciler Worker

The Arq worker entrypoint. Handles RunPod status polling, webhook processing, lease expiry, health probing, and all scheduled maintenance jobs.

#### Key functions

`validate_redis_dsn(dsn: str) -> bool` (line 59)
: Returns True only if `dsn` is a parseable `redis://` URL with a non-empty netloc.

`check_redis_config() -> int` (line 70)
: CLI check entrypoint. Prints `REDIS_URL is valid` on success, descriptive error on failure. Exits 0 or 1.

`map_runpod_status(status: str, *, cost_per_hr: Decimal|None = None, worker_time_ms: int|None = None, completed_at: datetime|None = None) -> RunPodJobStatus` (line 97)
: Maps a RunPod queue status string to a `RunPodJobStatus`. Terminal statuses `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`, `TIMEOUT`, `TIME_OUT` map to corresponding `WorkloadState` values via `_RUNPOD_TERMINAL_MAP` (line 45). Active statuses `IN_QUEUE`, `IN_PROGRESS` and unknown strings return `terminal=False`. Actual cost is computed from `cost_per_hr * worker_time_ms / 3_600_000` when both args are provided.

`_compute_actual_cost(cost_per_hr: Decimal|None, worker_time_ms: int|None) -> Decimal|None` (line 126)
: Internal. Returns `cost_per_ms * worker_time_ms` quantized to 6 decimal places, or `None` if either input is missing/zero.

`fetch_active_workloads(pool: asyncpg.Pool) -> list[dict[str, Any]]` (line 251)
: Returns workloads in `queued` or `running` state that have a non-null `runpod_job_id`. Columns returned: `id`, `runpod_job_id`.

`apply_terminal_state(pool: asyncpg.Pool, *, workload_id: str, state: WorkloadState, actual_cost: Decimal|None, completed_at: datetime) -> bool` (line 265)
: Updates `pitwall.workloads` with resolved state, cost, and completion time. Guards against re-applying to already-terminal rows via the `WHERE state NOT IN ('completed', 'failed', 'cancelled', 'timed_out')` clause. Returns `True` if a row was updated.

`fetch_workload_by_id(pool: asyncpg.Pool, workload_id: str) -> dict[str, Any]|None` (line 292)
: Fetches a single workload row by its `id`. Returns `None` if not found.

`fetch_workload_by_runpod_job_id(pool: asyncpg.Pool, runpod_job_id: str) -> dict[str, Any]|None` (line 305)
: Fetches a workload row by its `runpod_job_id`. Used by the webhook processing path.

`build_workload_completed_event(workload: dict[str, Any]) -> dict[str, Any]` (line 318)
: Serializes a workload row into a Redis pub/sub event payload. Keys: `event="workload.completed"`, `workload_id`, `capability_id`, `provider_id`, `state`, `completed_at`, `execution_ms`, `output_bytes`, `cost_actual_usd`, optional `error`, `result`, `fallback_chain`. Strips `Decimal` from `cost_actual_usd` via `str()`.

`publish_workload_completed(redis: Any, event: dict[str, Any]) -> int` (line 358)
: JSON-serializes `event` and publishes to channel `pitwall:workload:completed`. Returns subscriber count or 0 on error. No-op if `redis is None`.

`apply_terminal_status_and_publish(pool: asyncpg.Pool, redis: Any, runpod_job_id: str, status: str, completed_at: datetime|None = None) -> bool` (line 385)
: Core webhook-to-state function. Fetches workload by `runpod_job_id`, maps the status, applies terminal state, fetches the updated row, builds the event, and publishes it. Returns `True` if the workload was updated. Both `_poll_and_reconcile` and `_process_webhook_terminal_status` call this.

`aggregate_daily_cost(pool: asyncpg.Pool) -> None` (line 433)
: Runs `_AGGREGATE_DAILY_SQL` which upserts into `pitwall.cost_daily` from completed workload rows, joined through `capabilities` and `providers`. Groups by UTC day, capability class, and provider type.

`fetch_providers_for_health_probe(pool: asyncpg.Pool) -> list[dict[str, Any]]` (line 444)
: Returns enabled `serverless_lb` providers with a `runpod_endpoint_id`. Columns: `id`, `name`, `provider_type`, `runpod_endpoint_id`, `health_status`, `consecutive_failures`, `cooldown_trips`, `cooldown_until`.

`fetch_lb_providers_for_hibernate_sweep(pool: asyncpg.Pool) -> list[dict[str, Any]]` (line 459)
: Same filter as above but includes `config` column for the hibernate sweep logic.

`update_provider_health(pool: asyncpg.Pool, *, provider_id: str, health_status: str, consecutive_failures: int, cooldown_trips: int, cooldown_until: datetime|None) -> None` (line 473)
: Persists probe results back to the `pitwall.providers` row.

`_cost_reconcile(ctx: dict[str, Any]) -> None` (line 499) — **cron every 5 min**
: For each active workload, unconditionally maps `IN_PROGRESS` to a terminal state and applies it. This catches jobs RunPod marked IN_PROGRESS that silently completed without a webhook. Idempotent: skip-already-terminal guard on the UPDATE.

`_poll_and_reconcile(ctx: dict[str, Any]) -> None` — **cron every 2 min** (line 533)
: Polls all active workloads against RunPod. For `serverless_queue` providers uses `QueueClient.status()`; for `pod_lease` providers uses `get_pod()` and reads `runtime.podStatus`. Maps the returned status string; applies terminal state if terminal. Publishes `workload.completed` event on update. Silently continues on errors (inner try/except).

`_idempotency_gc(ctx: dict[str, Any]) -> None` — **cron nightly at 03:00 UTC** (line 608)
: Deletes workload rows with a non-null `idempotency_key` older than 24 hours.

`_lb_endpoint_hibernate_sweep(ctx: dict[str, Any]) -> None` — **cron daily at 12:00 UTC** (line 617)
: Per L14 invariant: endpoints with `workersMin > 0` that have been warm for > 24h trigger a `HibernateSweepAlert`. Tracks state in Redis (`pitwall:hibernate_sweep:workers_min:{provider_id}`) with 7-day TTL. Does NOT auto-hibernate — only alerts. Imports `L14_DAILY_BURN_PER_WORKER_USD` from `pitwall.cost.hibernate_alerts`.

`_backup_drill(ctx: dict[str, Any]) -> None` — **cron weekly Sun 04:00 UTC** (line 736)
: Delegates to `run_pit_restore_drill` in `pitwall.ops.backup_drill`. Silently suppresses all exceptions.

`_archive_old_workloads(ctx: dict[str, Any]) -> None` — **cron weekly Sun 05:00 UTC** (line 744)
: Archives completed workloads older than the retention threshold to JSONL files under `PITWALL_ARCHIVE_DIR`. Delegates to `archive_workloads_to_jsonl` in `pitwall.retention`.

`_rollup_job(ctx: dict[str, Any]) -> None` — **cron daily at 01:00 UTC** (line 759)
: Calls `run_rollup(pool)`. After rollup completes, invokes `after_rollup_hook` which calls `check_and_send_budget_alert`.

`_health_probe(ctx: dict[str, Any]) -> None` — **cron every minute** (line 777)
: Probes all non-cooldown `serverless_lb` providers via `LBClient.probe()`. Feeds results into `apply_probe_result` (from `pitwall.routing.cooldown`) to compute the next health state, then persists with `update_provider_health`.

`_lease_expiry_reconcile(ctx: dict[str, Any]) -> None` — **cron every minute** (line 810)
: Fetches active leases with `auto_teardown_on_expiry=True` expiring within 60 minutes. Publishes `lease.expiring` warning events at configurable intervals (default 15min and 5min before expiry via `PITWALL_LEASE_ADVANCE_WARNING_MIN`). On expiry (`minutes_until_expiry <= 0`), calls `run_teardown` with `LeaseState.EXPIRED`.

`_process_webhook_terminal_status(ctx: dict[str, Any], runpod_job_id: str, status: str) -> None` — **Arq job** (line 901)
: Enqueued by the webhook receiver. Applies terminal state and publishes the event. Calls `apply_terminal_status_and_publish` directly.

#### Invariants

- Terminal states are **never** overwritten: the UPDATE guard `state NOT IN ('completed', 'failed', 'cancelled', 'timed_out')` ensures idempotent re-runs.
- Cost is computed from RunPod-reported `worker_time_ms` multiplied by the provider's `cost_per_hr` rate; no estimation occurs.
- The `_poll_and_reconcile` loop is safe to re-run after a worker restart — all paths check the terminal guard before applying.

---

### `src/pitwall/reconciler/__main__.py` — CLI Entry Point

`main() -> None` (line 18)
: Supports two modes: `python -m pitwall.reconciler` (run worker) and `python -m pitwall.reconciler check` (validate Redis DSN). Exits 0 on success, 1 on config error or missing `arq`.

---

### `src/pitwall/reconciler/cost_daily_rollup.py` — Daily Cost Aggregation

`run_rollup(pool: asyncpg.Pool, *, after_rollup: AlertHook|None = None) -> None` (line 64)
: Runs two upsert statements:
1. `_AGGREGATE_DAILY_SQL`: joins `pitwall.workloads` → `pitwall.capabilities` → `pitwall.providers`, groups completed workloads by UTC day / capability class / provider type, upserts counts and sum of `cost_actual_usd` into `pitwall.cost_daily`.
2. `_VOLUME_STORAGE_DAILY_SQL`: accrues daily volume storage cost for volumes without a configured `monthly_cost_usd`. Tiered: $0.07/GB/mo ≤ 1 TB, $0.05/GB/mo > 1 TB. Upserts into `pitwall.volume_cost_daily`.

`AlertHook = Callable[[], Awaitable[Any]]` (line 61)
: Callback type for side-effects after a successful rollup. Used by `_rollup_job` to trigger budget alerts.

**Invariant**: Both statements are idempotent via `ON CONFLICT (day, ...)` clauses. Safe to re-run for the same UTC day.

---

### `src/pitwall/workload_lifecycle.py` — Workload State Machine

Provides pure functions for inserting and transitioning workload rows through their lifecycle. Used by the API layer when a request first arrives and when the worker reports completion.

`generate_workload_id() -> str` (line 38)
: Returns `f"wkl_{ulid_new()}"` — a ULID-based workload identifier.

`insert_passthrough_workload(repo: WorkloadRepository, *, workload_id: str, capability_id: str, provider_id: str, idempotency_key: str|None = None, input_data: dict[str, Any]|None = None, input_bytes: int|None = None) -> Workload` (line 42)
: Creates a `Workload` row in `QUEUED` state with `submitted_at` set to now. The row is inserted via `repo.insert()`.

`transition_to_running(repo: WorkloadRepository, workload_id: str, *, provider_id: str|None = None, fallback_chain: list[str]|None = None) -> Workload|None` (line 67)
: Calls `repo.guarded_transition` moving from `QUEUED` → `RUNNING`. Sets `started_at`. Optional `fallback_chain` is recorded on the row.

`transition_to_completed(repo: WorkloadRepository, workload_id: str, *, execution_ms: int|None = None, output_bytes: int|None = None, result: dict[str, Any]|None = None, fallback_chain: list[str]|None = None, langfuse_trace_id: str|None = None) -> Workload|None` (line 86)
: Calls `repo.guarded_transition` moving from `RUNNING` → `COMPLETED`. Sets `completed_at`, `execution_ms`, `output_bytes`, `result`, `fallback_chain`, `langfuse_trace_id`.

`transition_to_failed(repo: WorkloadRepository, workload_id: str, *, execution_ms: int|None = None, error: dict[str, Any]|None = None, fallback_chain: list[str]|None = None, langfuse_trace_id: str|None = None) -> Workload|None` (line 113)
: Calls `repo.guarded_transition` moving from `RUNNING` → `FAILED`. Sets `completed_at`, `execution_ms`, `error`, `fallback_chain`, `langfuse_trace_id`.

`enqueue_submit_runpod_job(workload_id: str) -> None` (line 138)
: Creates an ArqRedis connection from `REDIS_URL` and enqueues a `"submit_runpod_job"` job with `workload_id` as the sole argument. No-op if `arq` is unavailable or `REDIS_URL` is unset.

**Invariant**: `guarded_transition` only succeeds if the workload is in `from_states`. This enforces a strict state machine: `QUEUED` → `RUNNING` → `COMPLETED|FAILED`. Retrograde transitions are impossible.

---

### `src/pitwall/worker.py` — deferred-worker tombstone

The incomplete GPU worker is not an alpha feature. `main()` prints an
actionable ADR reference and returns `EX_UNAVAILABLE` (69), preventing stale
automation from treating a no-op process as healthy. See
[ADR 0002](../decisions/0002-worker-deferred.md).

---

## 3. Reconciliation Loop

### State Convergence Target

The reconciler converges all workloads to a terminal `WorkloadState` (`completed`, `failed`, `cancelled`, `timed_out`). The ground truth is the RunPod API response; Pitwall's DB is updated to match.

### Polling Mechanism

`_poll_and_reconcile` runs every 2 minutes. It queries all `queued`/`running` workloads with a `runpod_job_id`, then calls:
- `QueueClient.status(endpoint_id, job_id)` for `serverless_queue` providers.
- `get_pod(job_id)` for `pod_lease` providers (returns `TIMED_OUT` if the pod is `None`).

Results are mapped via `map_runpod_status`. Terminal statuses are applied via `apply_terminal_state`, then the updated workload is fetched and published to Redis.

### Webhook Path

RunPod sends webhooks on job completion. The webhook receiver enqueues `process_webhook_terminal_status` as an Arq job. The job calls `apply_terminal_status_and_publish`, which follows the same fetch → map → apply → publish path as polling.

### Terminal State Application

`apply_terminal_state` writes `state`, `cost_actual_usd`, and `completed_at` in a single UPDATE with a guard against already-terminal rows. The guard ensures that even if RunPod sends duplicate webhook events or the polling loop races, the state is set exactly once.

### Worker Entrypoint

```
python -m pitwall.reconciler          # Arq worker with cron jobs
python -m pitwall.reconciler check    # Redis DSN validation
```

The reconciler is driven by Arq's `WorkerSettings` class. Arq reads `REDIS_URL`, connects, and dispatches cron jobs and enqueued jobs.

---

## 4. Public Interfaces

### `pitwall.workload_lifecycle`

| Function | Signature |
|---|---|
| `generate_workload_id` | `() -> str` |
| `insert_passthrough_workload` | `(repo: WorkloadRepository, *, workload_id: str, capability_id: str, provider_id: str, idempotency_key: str\|None = None, input_data: dict[str, Any]\|None = None, input_bytes: int\|None = None) -> Workload` |
| `transition_to_running` | `(repo: WorkloadRepository, workload_id: str, *, provider_id: str\|None = None, fallback_chain: list[str]\|None = None) -> Workload\|None` |
| `transition_to_completed` | `(repo: WorkloadRepository, workload_id: str, *, execution_ms: int\|None = None, output_bytes: int\|None = None, result: dict[str, Any]\|None = None, fallback_chain: list[str]\|None = None, langfuse_trace_id: str\|None = None) -> Workload\|None` |
| `transition_to_failed` | `(repo: WorkloadRepository, workload_id: str, *, execution_ms: int\|None = None, error: dict[str, Any]\|None = None, fallback_chain: list[str]\|None = None, langfuse_trace_id: str\|None = None) -> Workload\|None` |
| `enqueue_submit_runpod_job` | `(workload_id: str) -> None` |

### `pitwall.reconciler`

| Function | Signature |
|---|---|
| `validate_redis_dsn` | `(dsn: str) -> bool` |
| `check_redis_config` | `() -> int` |
| `map_runpod_status` | `(status: str, *, cost_per_hr: Decimal\|None = None, worker_time_ms: int\|None = None, completed_at: datetime\|None = None) -> RunPodJobStatus` |
| `apply_terminal_state` | `(pool: asyncpg.Pool, *, workload_id: str, state: WorkloadState, actual_cost: Decimal\|None, completed_at: datetime) -> bool` |
| `fetch_workload_by_id` | `(pool: asyncpg.Pool, workload_id: str) -> dict[str, Any]\|None` |
| `fetch_workload_by_runpod_job_id` | `(pool: asyncpg.Pool, runpod_job_id: str) -> dict[str, Any]\|None` |
| `apply_terminal_status_and_publish` | `(pool: asyncpg.Pool, redis: Any, runpod_job_id: str, status: str, completed_at: datetime\|None = None) -> bool` |
| `publish_workload_completed` | `(redis: Any, event: dict[str, Any]) -> int` |
| `aggregate_daily_cost` | `(pool: asyncpg.Pool) -> None` |
| `fetch_providers_for_health_probe` | `(pool: asyncpg.Pool) -> list[dict[str, Any]]` |
| `update_provider_health` | `(pool: asyncpg.Pool, *, provider_id: str, health_status: str, consecutive_failures: int, cooldown_trips: int, cooldown_until: datetime\|None) -> None` |

### `pitwall.reconciler.cost_daily_rollup`

| Function | Signature |
|---|---|
| `run_rollup` | `(pool: asyncpg.Pool, *, after_rollup: AlertHook\|None = None) -> None` |

---

## 5. Configuration

| Environment Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL` | *(required)* | Arq Redis connection DSN, e.g. `redis://localhost:6379/0` |
| `RUNPOD_API_KEY` | *(required for polling/probes)* | RunPod REST API key |
| `RUNPOD_REST_API_URL` | `https://rest.runpod.io/v1` | RunPod API base URL for hibernate sweep |
| `PITWALL_LEASE_ADVANCE_WARNING_MIN` | `15,5` | Comma-separated minutes at which to fire lease expiry warnings |
| `PITWALL_ARCHIVE_DIR` | *(none)* | Directory path for JSONL workload archives |

The reconciler module calls `require_runtime_env("reconciler")` at import time (line 30), which validates that the reconciler runtime environment is present. `arq` must also be importable.

---

## 6. Failure Modes & Error Types

### Import errors
`workload_lifecycle.py` uses `try/except ImportError` around `arq` (lines 24–33). If `arq` is unavailable, `enqueue_submit_runpod_job` no-ops and logs a warning. The reconciler similarly guards `_ARQ_AVAILABLE` (lines 32–42).

### Database connection errors
All async DB operations (`fetch`, `execute`, `fetchrow`) are wrapped in try/except at the call site inside each cron job function. Errors are suppressed and the function returns early. This prevents one failing job from crashing the worker loop.

### RunPod API errors
`_poll_and_reconcile` wraps provider API calls in a try/except (lines 563–580). On any exception it `continue`s to the next workload. This means a RunPod outage causes polling to silently skip, not crash.

### Redis pub/sub errors
`publish_workload_completed` catches `Exception` and returns 0, ensuring a Redis failure does not propagate.

### Missing `runpod_job_id`
`_poll_and_reconcile` skips any row where `runpod_endpoint_id` or `runpod_job_id` is falsy (line 559–560).

### Lease teardown errors
`run_teardown` is called with `suppress(Exception)` at the call site in `_lease_expiry_reconcile` (line 857). If teardown fails, the lease remains active and the reconciler will retry on the next cron run.

### Webhook job not found
`apply_terminal_status_and_publish` returns `False` when `fetch_workload_by_runpod_job_id` returns `None`, indicating the workload is not yet in the DB or the job ID is unknown. The webhook receiver handles this gracefully.

### Terminal guard re-application
`apply_terminal_state` returns `False` if the workload was already terminal. All callers treat `False` as a no-op and do not publish duplicate events.

---

## 7. Testing

| Test file | What it covers |
|---|---|
| `tests/test_workload_lifecycle.py` | Workload state transition functions, `insert_passthrough_workload`, `transition_to_running/completed/failed`, `enqueue_submit_runpod_job` |
| `tests/reconciler/test_poll_and_reconcile.py` | `_poll_and_reconcile`, `map_runpod_status`, `apply_terminal_state`, `apply_terminal_status_and_publish` |
| `tests/reconciler/test_cost_daily_rollup.py` | `run_rollup`, daily aggregation SQL, volume storage accrual |
| `tests/reconciler/test_idempotency_gc.py` | `_idempotency_gc`, stale idempotency key deletion |
| `tests/reconciler/test_lease_expiry_reconcile.py` | `_lease_expiry_reconcile`, lease warning events, teardown trigger |
| `tests/reconciler/test_lb_endpoint_hibernate_sweep.py` | `_lb_endpoint_hibernate_sweep`, L14 warm-duration tracking, alert threshold |
| `tests/reconciler/test_init_coverage.py` | Surface API coverage check for the reconciler `__init__` module |
| `tests/api/test_e2e_lease_lifecycle.py` | End-to-end lease lifecycle (teardown path exercised via reconciler) |

---

## 8. Dependencies

### From `pitwall` itself

| Import | Source module |
|---|---|
| `WorkloadState`, `LeaseState` enums | `pitwall.core.enums` |
| `Workload` model | `pitwall.core.models` |
| `ulid_new` | `pitwall.core.ids` |
| `WorkloadRepository` | `pitwall.db.repository` |
| `require_runtime_env` | `pitwall.config` |
| `QueueClient` | `pitwall.runpod_client.queue` |
| `get_pod` | `pitwall.runpod_client.pods` |
| `LBClient` | `pitwall.runpod_client.lb` |
| `apply_probe_result`, `is_in_cooldown` | `pitwall.routing.cooldown` |
| `HibernateSweepAlert`, `send_hibernate_sweep_alert`, `L14_DAILY_BURN_PER_WORKER_USD` | `pitwall.cost.hibernate_alerts` |
| `check_and_send_budget_alert` | `pitwall.cost.alerts` |
| `run_teardown` | `pitwall.api.leases.teardown` |
| `run_pit_restore_drill` | `pitwall.ops.backup_drill` |
| `archive_workloads_to_jsonl` | `pitwall.retention` |

### External libraries

| Library | Used for |
|---|---|
| `arq` | Arq worker, cron scheduling, job enqueuing |
| `asyncpg` | PostgreSQL async driver for all DB operations |
| `pydantic` | `RunPodJobStatus` model |
| `httpx` | HTTP client for RunPod REST API (hibernate sweep, health probes) |
| `datetime`, `decimal` | Cost calculations and timestamping |
| `pathlib` | Archive directory path resolution |
| `json` | Redis pub/sub payload serialization |
