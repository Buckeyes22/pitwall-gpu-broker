# 07 — Data Model, Schema & Persistence

## 1. Purpose & Scope

The data-model subsystem owns all PostgreSQL persistence for Pitwall: connection-pool management, migration discovery and application, and the async repository layer that wraps raw SQL. It provides the persistence foundation for every other subsystem — the API, reconciler, worker, cost engine, and webhook dispatcher all acquire the shared `asyncpg.Pool` and call repository methods to persist registry mutations, workload lifecycle events, lease state, kill-switch events, and drill evidence.

## 2. Components

### `src/pitwall/db/__init__.py`

Entry point for pool lifecycle management and the `pitwall-gpu-broker db` CLI.

**Pool management functions:**

- `async get_pool(dsn: str | None = None, *, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool` — singleton pool factory. Reads `DATABASE_URL` from the environment when `dsn` is not provided. Sets `statement_cache_size=0` for PgBouncer transaction-mode compatibility and registers the JSONB codec on every new connection via `init=_register_codecs`. `global _pool`.
- `async close_pool() -> None` — closes the singleton pool.
- `async def db_lifespan(app: FastAPI) -> AsyncIterator[None]` — FastAPI lifespan context manager. On startup creates the pool and attaches it to `app.state.pool`; on shutdown closes it.
- `async def get_db_pool(request: Request) -> asyncpg.Pool` — FastAPI dependency that retrieves `app.state.pool`. Raises `RuntimeError` if uninitialised. Returns `DbPoolDep` (an `Annotated[asyncpg.Pool, Depends(get_db_pool)]`).

**JSONB codec:**

- `_encode_jsonb(value: object) -> str` — passes strings through unchanged; calls `json.dumps()` on dict/list. Prevents double-encoding when callers pass pre-serialized JSON strings.
- `async _register_codecs(conn: asyncpg.Connection) -> None` — registers the JSONB type codec on every connection using `_encode_jsonb` as the encoder and `lambda value: json.loads(value)` as the decoder, with `format="text"`. Registered in `create_pool(init=...)`.

**CLI commands (called from `main()`):**

- `def cmd_migrate() -> int` — discovers `db/migrations/*.sql`, creates `pitwall.schema_migrations` if absent, applies pending migrations through the existing `asyncpg` pool, records each applied migration's version/filename/checksum, then closes the CLI pool. Uses `ON CONFLICT (version) DO UPDATE` for idempotency and does not require a local `psql` binary.
- `def cmd_reset() -> int` — drops the entire `pitwall` schema (`DROP SCHEMA pitwall CASCADE`).
- `def cmd_status() -> int` — prints applied/pending status of all discoverable migrations without applying any.

**Helper functions:**

- `_database_url() -> str` — reads `DATABASE_URL` env var or exits with `SystemExit(1)`.
- `_psql_available(database_url: str) -> str | None` — returns path to `psql` if the probe query succeeds; otherwise `None`.
- `_docker_psql(database_url: str) -> list[str] | None` — returns a `docker exec -i psql ...` command vector targeting `_TEST_POSTGRES_CONTAINER = "pitwall-test-postgres"` if that container is running; otherwise `None`.
- `async _cmd_migrate_async(database_url: str) -> int` — async implementation behind `cmd_migrate`; applies each pending migration and its tracking insert in one transaction.
- `async _applied_migrations_async(pool: asyncpg.Pool) -> dict[str, str]` — queries `pitwall.schema_migrations` through asyncpg and returns `version -> checksum` mapping.
- `_run_sql(database_url: str, sql: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]` — executes `sql` via `psql` (preferred) or Docker `psql`, with `ON_ERROR_STOP=1` and a 60 s timeout. Kept for `cmd_reset` and `cmd_status`.
- `_applied_migrations(database_url: str) -> dict[str, str]` — psql-backed status helper that returns `version -> checksum` mapping.

### `src/pitwall/db/repository.py`

Async repository interfaces for all Pitwall registry tables. Every public method returns a Pydantic model instance, never a raw `asyncpg.Record`.

**Row unmarshal helpers** (private module-level functions):

- `_capability_from_row(row: asyncpg.Record) -> Capability`
- `_provider_from_row(row: asyncpg.Record) -> Provider`
- `_workload_from_row(row: asyncpg.Record) -> Workload`
- `_lease_from_row(row: asyncpg.Record) -> Lease`
- `_endpoints_from_row(row: asyncpg.Record) -> LeaseEndpoints | None`
- `_readiness_from_row(row: asyncpg.Record) -> LeaseReadiness | None`
- `_audit_from_row(row: asyncpg.Record) -> ConfigAuditEntry`
- `_webhook_delivery_failure_from_row(row: asyncpg.Record) -> WebhookDeliveryFailure`
- `_webhook_subscription_from_row(row: asyncpg.Record) -> WebhookSubscription`

**`CapabilityRepository`**

Constructor: `def __init__(self, pool: asyncpg.Pool) -> None`

| Method | Signature |
|--------|-----------|
| `create` | `async def create(self, cap: Capability) -> Capability` |
| `get` | `async def get(self, capability_id: str) -> Capability \| None` |
| `get_by_name` | `async def get_by_name(self, name: str) -> Capability \| None` |
| `list` | `async def list(self, *, enabled_only: bool = False, class_filter: str \| None = None, limit: int = 100, offset: int = 0) -> list[Capability]` |
| `patch` | `async def patch(self, capability_id: str, *, name: str \| None = None, version: str \| None = None, class_: str \| None = None cost_mode: str \| None = None, config: JsonObject \| None = None, source: str \| None = None, last_applied_yaml_hash: str \| None \| object = _UNSET) -> Capability \| None` |
| `enable` | `async def enable(self, capability_id: str) -> Capability \| None` |
| `disable` | `async def disable(self, capability_id: str) -> Capability \| None` |
| `upsert` | `async def upsert(self, name: str, class_: str, cost_mode: str, *, version: str = "1.0.0", description: str \| None = None, input_schema: JsonObject \| None = None, output_schema: JsonObject \| None = None, hints_supported: list[str] \| None = None, openai_compatible: bool = False, enabled: bool = True) -> Capability` |

`create` and `upsert` cast `config` to `::jsonb` at the driver level (asyncpg serialises dicts natively; explicit `::jsonb` cast is used for clarity). `patch` uses `_UNSET` sentinel to distinguish "not passed" from `None` for nullable fields; `_CLEARABLE_PROVIDER_FIELDS = {"cooldown_until", "last_applied_yaml_hash"}` are cleared when passed as `None`.

**`ProviderRepository`**

Constructor: `def __init__(self, pool: asyncpg.Pool) -> None`

| Method | Signature |
|--------|-----------|
| `create` | `async def create(self, provider: Provider) -> Provider` |
| `get` | `async def get(self, provider_id: str) -> Provider \| None` |
| `get_by_name` | `async def get_by_name(self, name: str) -> Provider \| None` |
| `list` | `async def list(self, *, capability_id: str \| None = None, enabled_only: bool = False, provider_type: str \| None = None, limit: int = 100, offset: int = 0) -> list[Provider]` |
| `patch` | `async def patch(self, provider_id: str, *, name: str \| None = None, provider_type: str \| None = None, runpod_endpoint_id: str \| None \| object = _UNSET, runpod_template_id: str \| None \| object = _UNSET, region: str \| None \| object = _UNSET, cloud_type: str \| None \| object = _UNSET, config: JsonObject \| None = None, priority: int \| None = None, health_status: str \| None = None, consecutive_failures: int \| None = None, cooldown_trips: int \| None = None, cold_start_p50_ms: int \| None \| object = _UNSET, cold_start_p95_ms: int \| None \| object = _UNSET, recent_error_rate: float \| None = None, cooldown_until: dt.datetime \| None \| object = _UNSET, source: str \| None = None, last_applied_yaml_hash: str \| None \| object = _UNSET) -> Provider \| None` |
| `enable` | `async def enable(self, provider_id: str) -> Provider \| None` |
| `disable` | `async def disable(self, provider_id: str) -> Provider \| None` |

**`LeaseRepository`**

Constructor: `def __init__(self, pool: asyncpg.Pool) -> None`

| Method | Signature |
|--------|-----------|
| `create` | `async def create(self, lease: Lease) -> Lease` |
| `get` | `async def get(self, lease_id: str) -> Lease \| None` |
| `update_state` | `async def update_state(self, lease_id: str, state: str) -> Lease \| None` |
| `close_teardown` | `async def close_teardown(self, lease_id: str, *, state: str, cost_accrued_usd: Any, terminated_at: dt.datetime, terminated_reason: str) -> Lease \| None` |
| `update_endpoints` | `async def update_endpoints(self, lease_id: str, endpoints: LeaseEndpoints) -> Lease \| None` |
| `update_readiness` | `async def update_readiness(self, lease_id: str, readiness: LeaseReadiness) -> Lease \| None` |
| `update_expires_at` | `async def update_expires_at(self, lease_id: str, expires_at: dt.datetime) -> Lease \| None` |

`create` uses `model_dump_json()` for `endpoints` and `readiness` JSONB fields. `close_teardown` atomically sets `state`, `cost_accrued_usd`, `terminated_at`, and `terminated_reason`.

**`WorkloadRepository`**

Constructor: `def __init__(self, pool: asyncpg.Pool) -> None`

| Method | Signature |
|--------|-----------|
| `insert` | `async def insert(self, workload: Workload) -> Workload` |
| `get` | `async def get(self, workload_id: str) -> Workload \| None` |
| `get_by_idempotency_key` | `async def get_by_idempotency_key(self, key: str) -> Workload \| None` |
| `update_state` | `async def update_state(self, workload_id: str, state: str \| WorkloadState, *, started_at: dt.datetime \| None = None, completed_at: dt.datetime \| None = None, execution_ms: int \| None = None, queue_ms: int \| None = None, result: JsonObject \| None = None, error: JsonObject \| None = None, output_bytes: int \| None = None, fallback_chain: list[str] \| None = None, langfuse_trace_id: str \| None = None) -> Workload \| None` |
| `guarded_transition` | `async def guarded_transition(self, workload_id: str, from_states: set[str], to_state: str \| WorkloadState, *, patch: dict[str, Any] \| None = None) -> Workload \| None` |

`guarded_transition` acquires a `SELECT ... FOR UPDATE` row lock inside a transaction, then performs `UPDATE ... WHERE state IN (from_states)`. Returns `None` if the workload was not in one of the allowed `from_states`. JSONB columns in `patch` are cast `::jsonb`.

**`WebhookDeliveryRepository`**

Constructor: `def __init__(self, pool: asyncpg.Pool) -> None`

| Method | Signature |
|--------|-----------|
| `insert_or_skip` | `async def insert_or_skip(self, runpod_job_id: str, attempt: int, payload: dict[str, Any]) -> WebhookDeliveryResult` |

Uses `ON CONFLICT (runpod_job_id, attempt) DO NOTHING` on the unique constraint as a dedupe gate. Returns `WebhookDeliveryResult(is_new: bool, delivery_id: int | None)`.

**`WebhookDeliveryFailureRepository`**

Constructor: `def __init__(self, pool: asyncpg.Pool) -> None`

| Method | Signature |
|--------|-----------|
| `insert` | `async def insert(self, workload_id: str, subscription_id: int, attempt: int, payload: dict[str, Any], *, next_retry_at: dt.datetime \| None = None, status_code: int \| None = None, error_message: str \| None = None) -> WebhookDeliveryFailure` |
| `get` | `async def get(self, workload_id: str, subscription_id: int, attempt: int) -> WebhookDeliveryFailure \| None` |
| `list_by_workload` | `async def list_by_workload(self, workload_id: str) -> list[WebhookDeliveryFailure]` |
| `list_pending_retries` | `async def list_pending_retries(self, before: dt.datetime, limit: int = 100) -> list[WebhookDeliveryFailure]` |
| `update_next_retry` | `async def update_next_retry(self, workload_id: str, subscription_id: int, attempt: int, next_retry_at: dt.datetime \| None) -> WebhookDeliveryFailure \| None` |

**`WebhookSubscriptionRepository`**

Constructor: `def __init__(self, pool: asyncpg.Pool) -> None`

| Method | Signature |
|--------|-----------|
| `create` | `async def create(self, consumer: str, webhook_url: str, *, hmac_secret: str \| None = None, active: bool = True) -> WebhookSubscription` |
| `get` | `async def get(self, subscription_id: int) -> WebhookSubscription \| None` |
| `list` | `async def list(self, *, consumer: str \| None = None, active_only: bool = False, limit: int = 100, offset: int = 0) -> list[WebhookSubscription]` |

**Audit helpers** (module-level async functions):

- `async def insert_audit(pool: asyncpg.Pool, *, actor: str, action: str, entity_type: str, entity_id: str, old_value: JsonObject | None = None, new_value: JsonObject | None = None, change_reason: str | None = None) -> ConfigAuditEntry`
- `async def list_audit(pool: asyncpg.Pool, *, entity_type: str | None = None, entity_id: str | None = None, action: str | None = None, limit: int = 50) -> list[ConfigAuditEntry]`

### `src/pitwall/migrations.py`

Migration discovery, checksum tracking, and drift detection. No side effects on import.

**`@dataclass(frozen=True) class MigrationRecord`** — fields: `version: str`, `filename: str`, `checksum: str`.

**`@dataclass(frozen=True) class DriftEntry`** — fields: `version: str`, `filename: str`, `recorded_checksum: str`, `current_checksum: str`.

**Functions:**

- `def default_migrations_dir() -> Path` — returns `Path(__file__).resolve().parent.parent.parent / "db" / "migrations"` (repo-root relative).
- `def discover_migrations(migrations_dir: Path | str | None = None) -> list[MigrationRecord]` — glob searches `*.sql`, sorts lexically, computes SHA-256 of each file, returns sorted records. Raises `FileNotFoundError` if the directory does not exist.
- `def detect_drift(expected: list[MigrationRecord], applied: dict[str, str]) -> list[DriftEntry]` — compares on-disk checksums against previously-recorded checksums in `schema_migrations`. Returns a non-empty list when any applied migration's checksum has changed.
- `def _sha256(data: bytes) -> str` — `hashlib.sha256(data).hexdigest()`.

### `src/pitwall/db/kill_log.py`

Async repository for kill-switch audit rows.

**Functions:**

- `async def persist_kill_report(pool: asyncpg.Pool, triggered_at: datetime, reason: str, actor: str, pods_terminated: int, total_duration_ms: int, errors: list[str], *, endpoints_hibernated: int = 0, workloads_cancelled: int = 0) -> int` — inserts into `pitwall.kill_log`, returns the inserted row `id`. `errors` is serialised as JSONB via `json.dumps()` at the call site (not via asyncpg codec registration).
- `async def get_recent_kill_reports(pool: asyncpg.Pool, *, since: datetime | None = None, reason_prefix: str | None = None, limit: int = 100) -> list[dict[str, Any]]` — returns raw row dicts ordered by `triggered_at DESC`.

### `src/pitwall/db/drill_evidence.py`

Dual persistence path for drill evidence: a `config_audit` row (authoritative) plus an on-disk JSON report.

**Functions:**

- `async def persist_drill_evidence(pool: asyncpg.Pool, drill_id: str, drill_type: str, evidence: dict[str, Any], *, actor: str = "system", change_reason: str | None = None) -> int` — inserts a `config_audit` row with `entity_type='drill'`. `evidence` is JSON-serialised before the `::jsonb` cast (the `::jsonb` cast on the wire expects a string or dict that PostgreSQL can parse, so `json.dumps(evidence)` is pre-applied).
- `def write_drill_json_report(evidence: dict[str, Any], *, drill_type: str | None = None, output_dir: str | Path | None = None) -> Path` — writes to `{output_dir}/{drill_type}-{YYYYMMDD-HHMMSS}.json`. Defaults `output_dir` from `PITWALL_DRILL_ARTIFACTS_DIR` env var, falling back to `artifacts/drils` relative to repo root.
- `async def get_drill_evidence(pool: asyncpg.Pool, *, drill_id: str | None = None, drill_type: str | None = None, since: datetime | None = None, limit: int = 100) -> list[dict[str, Any]]` — queries `config_audit` rows with `entity_type = 'drill'`, optionally filtering by `drill_id`, `drill_type` embedded in `new_value`, and `since`.

### `src/pitwall/db/__main__.py`

Thin entry-point: `python -m pitwall.db` delegates to `pitwall.db.main()`.

## 3. Schema

### Migration order (`db/migrations/*.sql`)

| File | Table / Object Added |
|------|----------------------|
| `0001_capabilities.sql` | `pitwall.capabilities` |
| `0002_providers.sql` | `pitwall.providers`, `pitwall.provider_gpu_type_priority_is_canonical()` CHECK function |
| `0003_workloads.sql` | `pitwall.workloads` |
| `0004_leases.sql` | `pitwall.leases`, `pitwall.lease_active_has_readiness_signals()` CHECK function |
| `0005_runpod_templates.sql` | `pitwall.runpod_templates` |
| `0006_kill_log.sql` | `pitwall.kill_log` |
| `0007_config_audit.sql` | `pitwall.config_audit` |
| `0008_volumes.sql` | `pitwall.volumes` |
| `0009_cost_daily.sql` | `pitwall.cost_daily` |
| `0010_rate_buckets.sql` | `pitwall.rate_buckets` |
| `0011_workload_cost_columns.sql` | Adds `cost_estimate_usd`/`cost_actual_usd` columns with non-negativity CHECKs; `idx_workloads_month_spend` |
| `0012_alert_events.sql` | `pitwall.alert_events` |
| `0013_provider_cooldown_state.sql` | Adds `consecutive_failures`, `cooldown_trips` columns and non-neg CHECKs to `providers`; `idx_providers_cooldown_until` |
| `0014_async_job_migration.sql` | `pitwall.idempotency_keys`, `pitwall.runpod_webhook_deliveries`, `pitwall.webhook_subscriptions` |
| `0015_webhook_delivery_failures.sql` | `pitwall.webhook_delivery_failures` |
| `0016_drill_entity_type.sql` | Adds `'drill'` to `config_audit.entity_type` CHECK |
| `0017_volume_cost_daily.sql` | `pitwall.volume_cost_daily` |
| `0018_lease_auto_teardown.sql` | Adds `auto_teardown_on_expiry` column to `leases` |
| `0019_lease_mutation_idempotency.sql` | Adds atomic lease-mutation idempotency records and lease audit constraints |
| `0020_webhook_security.sql` | Replaces plaintext webhook signing secrets with versioned AES-GCM ciphertext and audited lifecycle constraints |

### Table details

**`pitwall.capabilities`** (`0001_capabilities.sql`)
`id TEXT PK`, `name TEXT UNIQUE NOT NULL`, `version TEXT NOT NULL`, `class TEXT NOT NULL`, `cost_mode TEXT NOT NULL CHECK (cost_mode IN ('per_second','per_request','per_token'))`, `config JSONB NOT NULL`, `source TEXT NOT NULL DEFAULT 'api'`, `last_applied_yaml_hash TEXT`, `enabled BOOLEAN DEFAULT true`, `created_at TIMESTAMPTZ DEFAULT now()`, `updated_at TIMESTAMPTZ DEFAULT now()`

**`pitwall.providers`** (`0002_providers.sql`)
`id TEXT PK`, `capability_id TEXT REFERENCES pitwall.capabilities(id)`, `name TEXT NOT NULL`, `provider_type TEXT NOT NULL CHECK (provider_type IN ('serverless_queue','serverless_lb','public_endpoint','pod_lease'))`, `runpod_endpoint_id TEXT`, `runpod_template_id TEXT`, `region TEXT`, `cloud_type TEXT CHECK (cloud_type IN ('SECURE','COMMUNITY'))`, `config JSONB NOT NULL`, `priority INTEGER NOT NULL`, `enabled BOOLEAN DEFAULT true`, `health_status TEXT DEFAULT 'unknown'`, `cold_start_p50_ms INTEGER`, `cold_start_p95_ms INTEGER`, `recent_error_rate REAL DEFAULT 0`, `cooldown_until TIMESTAMPTZ`, `source TEXT NOT NULL DEFAULT 'api'`, `last_applied_yaml_hash TEXT`, `updated_at TIMESTAMPTZ DEFAULT now()`
Constraints: `providers_volume_requires_secure_cloud` (volume fields require `SECURE` cloud type), `providers_gpu_type_priority_canonical` (GPU type priority JSONB must satisfy the canonical function)
Index: `idx_providers_capability_priority` ON `(capability_id, priority)` WHERE `enabled = true`

**`pitwall.workloads`** (`0003_workloads.sql`)
`id TEXT PK`, `capability_id TEXT NOT NULL`, `provider_id TEXT NOT NULL`, `type TEXT NOT NULL`, `state TEXT NOT NULL CHECK (state IN ('queued','running','completed','failed','cancelled','timed_out'))`, `runpod_job_id TEXT`, `idempotency_key TEXT`, `input JSONB`, `result JSONB`, `fallback_chain TEXT[]`, `error JSONB`, `submitted_at TIMESTAMPTZ NOT NULL`, `started_at TIMESTAMPTZ`, `completed_at TIMESTAMPTZ`, `execution_ms INTEGER`, `queue_ms INTEGER`, `cold_start_ms INTEGER`, `input_bytes INTEGER`, `output_bytes INTEGER`, `cost_estimate_usd NUMERIC(12,6)`, `cost_actual_usd NUMERIC(12,6)`, `langfuse_trace_id TEXT`
Indexes: `idx_workloads_idempotency` UNIQUE ON `idempotency_key` WHERE `idempotency_key IS NOT NULL`; `idx_workloads_state_submitted` ON `(state, submitted_at DESC)`; `idx_workloads_month_spend` ON `(submitted_at)` WHERE `state IN ('queued','running','completed')`

**`pitwall.leases`** (`0004_leases.sql`)
`id TEXT PK`, `provider_id TEXT NOT NULL`, `runpod_pod_id TEXT NOT NULL`, `state TEXT NOT NULL CHECK (state IN ('creating','waiting_runtime','waiting_probe','active','stopping','stopped','failed','expired'))`, `created_at TIMESTAMPTZ NOT NULL`, `expires_at TIMESTAMPTZ NOT NULL`, `renewal_policy TEXT NOT NULL`, `endpoints JSONB`, `readiness JSONB`, `cost_accrued_usd NUMERIC(12,6)`, `last_health_at TIMESTAMPTZ`, `terminated_at TIMESTAMPTZ`, `terminated_reason TEXT`, `auto_teardown_on_expiry BOOLEAN NOT NULL DEFAULT true`
Constraints: `leases_expires_after_created` CHECK (`expires_at > created_at`), `leases_active_readiness_signals` CHECK (`pitwall.lease_active_has_readiness_signals(state, endpoints, readiness)`)
Index: `idx_leases_expires` ON `(state, expires_at)` WHERE `state = 'active'`

**`pitwall.runpod_templates`** (`0005_runpod_templates.sql`)
`id TEXT PK`, `runpod_template_id TEXT NOT NULL`, `name TEXT NOT NULL`, `image_sha TEXT NOT NULL`, `image_ref TEXT NOT NULL`, `registry_auth_id TEXT`, `container_disk_gb INTEGER NOT NULL DEFAULT 50`, `volume_mount_path TEXT NOT NULL DEFAULT '/workspace'`, `env_schema TEXT[]`, `created_at TIMESTAMPTZ DEFAULT now()`
Unique constraint: `(name, image_sha)`. Index: `idx_runpod_templates_image_sha` ON `image_sha`.

**`pitwall.kill_log`** (`0006_kill_log.sql`)
`id BIGSERIAL PK`, `triggered_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `reason TEXT NOT NULL`, `actor TEXT NOT NULL`, `pods_terminated INTEGER NOT NULL DEFAULT 0`, `endpoints_hibernated INTEGER NOT NULL DEFAULT 0`, `workloads_cancelled INTEGER NOT NULL DEFAULT 0`, `total_duration_ms INTEGER NOT NULL`, `errors JSONB NOT NULL DEFAULT '[]'::jsonb`
Index: `idx_kill_log_triggered` ON `(triggered_at DESC)`

**`pitwall.config_audit`** (`0007_config_audit.sql`, extended by `0016_drill_entity_type.sql`)
`id BIGSERIAL PK`, `actor TEXT NOT NULL CHECK (actor IN ('rest:admin','mcp:session-id','system'))`, `action TEXT NOT NULL CHECK (action IN ('create','update','delete','enable','disable','hibernate'))`, `entity_type TEXT NOT NULL CHECK (entity_type IN ('capability','provider','volume','template','drill'))`, `entity_id TEXT NOT NULL`, `old_value JSONB`, `new_value JSONB`, `change_reason TEXT`, `created_at TIMESTAMPTZ DEFAULT now()`
Index: `idx_audit_entity` ON `(entity_type, entity_id, created_at DESC)`

**`pitwall.volumes`** (`0008_volumes.sql`)
`id TEXT PK`, `runpod_volume_id TEXT NOT NULL UNIQUE`, `name TEXT NOT NULL`, `datacenter_id TEXT NOT NULL`, `size_gb INTEGER NOT NULL CHECK (size_gb > 0)`, `purpose TEXT`, `equivalent_to TEXT[]`, `sync_strategy TEXT`, `monthly_cost_usd NUMERIC(10,2)`, `config JSONB NOT NULL DEFAULT '{}'::jsonb`, `created_at TIMESTAMPTZ DEFAULT now()`
Index: `idx_volumes_datacenter` ON `(datacenter_id)`

**`pitwall.cost_daily`** (`0009_cost_daily.sql`)
`(day DATE, capability_class TEXT, provider_type TEXT) PK`, `workload_count INTEGER NOT NULL`, `cost_usd NUMERIC(12,6) NOT NULL`

**`pitwall.rate_buckets`** (`0010_rate_buckets.sql`)
`(endpoint_id TEXT, operation TEXT) PK`, `capacity INTEGER NOT NULL CHECK (capacity > 0)`, `tokens REAL NOT NULL CHECK (tokens >= 0)`, `last_refilled_at TIMESTAMPTZ NOT NULL`, `recent_429_at TIMESTAMPTZ`

**`pitwall.idempotency_keys`** (`0014_async_job_migration.sql`)
`idempotency_key TEXT PK`, `workload_id TEXT NOT NULL`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
Index: `idx_idempotency_keys_created_at` ON `(created_at)`

**`pitwall.runpod_webhook_deliveries`** (`0014_async_job_migration.sql`)
`id BIGSERIAL PK`, `runpod_job_id TEXT NOT NULL`, `attempt INTEGER NOT NULL CHECK (attempt >= 1 AND attempt <= 3)`, `received_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `payload JSONB`
Unique constraint: `(runpod_job_id, attempt)`. Index: `idx_runpod_webhook_deliveries_received_at` ON `(received_at)`

**`pitwall.webhook_subscriptions`** (`0014_async_job_migration.sql`)
`id BIGSERIAL PK`, `consumer TEXT NOT NULL`, `webhook_url TEXT NOT NULL`, `hmac_secret TEXT`, `active BOOLEAN NOT NULL DEFAULT true`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
Index: `idx_webhook_subscriptions_consumer` ON `(consumer, active)`

**`pitwall.webhook_delivery_failures`** (`0015_webhook_delivery_failures.sql`)
`id BIGSERIAL PK`, `workload_id TEXT NOT NULL`, `subscription_id BIGINT NOT NULL REFERENCES pitwall.webhook_subscriptions(id)`, `attempt INTEGER NOT NULL CHECK (attempt >= 1 AND attempt <= 4)`, `attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `next_retry_at TIMESTAMPTZ`, `payload JSONB NOT NULL`, `status_code INTEGER`, `error_message TEXT`
Unique constraint: `(workload_id, subscription_id, attempt)`. Indexes: `idx_webhook_delivery_failures_workload_id`, `idx_webhook_delivery_failures_subscription_id`, `idx_webhook_delivery_failures_next_retry_at`

**`pitwall.alert_events`** (`0012_alert_events.sql`)
`(month TEXT, threshold_pct INTEGER) PK`, `sent_at TIMESTAMPTZ NOT NULL DEFAULT now()`

**`pitwall.volume_cost_daily`** (`0017_volume_cost_daily.sql`)
`(day DATE, volume_id TEXT) PK`, `cost_usd NUMERIC(12,6) NOT NULL`, `size_gb INTEGER NOT NULL`, `tiered_rate_per_gb NUMERIC(10,6) NOT NULL`, `volume_id TEXT REFERENCES pitwall.volumes(id)`
Indexes: `idx_volume_cost_daily_volume` ON `(volume_id)`, `idx_volume_cost_daily_day` ON `(day DESC)`

### Codec rules

**JSONB** — registered once per connection via `asyncpg.connection.set_type_codec('jsonb', schema='pg_catalog', encoder=_encode_jsonb, decoder=lambda value: json.loads(value), format='text')`. The encoder passes string values through unchanged to prevent double-encoding of pre-serialized payloads. All JSONB fields in repository INSERT/UPDATE statements use explicit `::jsonb` SQL casts at the driver level.

**NUMERIC(12,6)** — `cost_estimate_usd`, `cost_actual_usd`, `cost_accrued_usd`, `cost_usd`, `tiered_rate_per_gb`. Stored as `NUMERIC(12,6)` to preserve six decimal places of USD precision (avoiding floating-point drift). The asyncpg driver returns `NUMERIC` as `Decimal`.

**NUMERIC(10,2)** — `monthly_cost_usd` on volumes. Two decimal places.

## 4. Public Interfaces

```
# Pool & lifespan
from pitwall.db import get_pool, close_pool, db_lifespan, get_db_pool, DbPoolDep

# Repository classes
from pitwall.db.repository import (
    CapabilityRepository,
    ProviderRepository,
    LeaseRepository,
    WorkloadRepository,
    WebhookDeliveryRepository,
    WebhookDeliveryFailureRepository,
    WebhookSubscriptionRepository,
    insert_audit,
    list_audit,
)

# Kill log
from pitwall.db.kill_log import persist_kill_report, get_recent_kill_reports

# Drill evidence
from pitwall.db.drill_evidence import persist_drill_evidence, write_drill_json_report, get_drill_evidence

# Migration management
from pitwall.migrations import discover_migrations, detect_drift, MigrationRecord, DriftEntry

# CLI entry point
from pitwall.db import main
python -m pitwall.db {migrate,reset,status}
```

## 5. Configuration

| Environment Variable | Default | Purpose |
|----------------------|---------|---------|
| `DATABASE_URL` | *(required)* | PostgreSQL connection string. Read by `get_pool()` and all `cmd_*` functions. |
| `PITWALL_DRILL_ARTIFACTS_DIR` | `artifacts/drils` (repo-relative) | Directory for on-disk drill JSON reports. |
| `PITWALL_DATABASE_URL` | *(optional, backup drills only)* | Separate source DB URL for backup drill restore validation. |

Pool sizing: `min_size=2`, `max_size=10` (configured inline in `get_pool()`; not externally configurable).

## 6. Failure Modes & Error Types

| Condition | Behaviour |
|-----------|-----------|
| `DATABASE_URL` unset in `get_pool()` | `AssertionError: dsn or DATABASE_URL environment variable is required` |
| `DATABASE_URL` unset in CLI commands | Prints `"DATABASE_URL is not set"` to stderr; `SystemExit(1)` |
| `app.state.pool` not set in `get_db_pool()` dependency | `RuntimeError: Database pool not initialized. Did you forget to use db_lifespan?` |
| `cmd_migrate` cannot execute through asyncpg | Prints an asyncpg/DATABASE_URL compatibility error; returns `1` |
| No `psql` binary and Docker test container not running during `cmd_reset`/`cmd_status` | Prints guidance to stderr; `SystemExit(1)` |
| Migration file changed after application (drift) | `detect_drift()` returns non-empty list; caller is expected to reject the migration run |
| Migration table missing and `cmd_status` called first | Prints guidance; returns `1` |
| `Repository.get()` finds no row | Returns `None` (not an exception) |
| `LeaseRepository.create` with missing `auto_teardown_on_expiry` column (pre-0018 schema) | `psycopg2.errors.UndefinedColumnError` at INSERT |
| JSONB double-encoding — passing a pre-serialized JSON string to a repository method | Decoder returns the raw JSON string instead of a dict; callers that expect `isinstance(result, dict)` will get `None` |
| `WebhookDeliveryRepository.insert_or_skip` on duplicate `(runpod_job_id, attempt)` | Returns `WebhookDeliveryResult(is_new=False, delivery_id=None)`; no exception raised |

## 7. Testing

| Test File | What It Covers |
|----------|---------------|
| `tests/integration/test_migrations.py` | Applies all `db/migrations/*.sql` files via `discover_migrations` order; verifies clean, idempotent application |
| `tests/integration/test_lease_jsonb_roundtrip.py` | `LeaseRepository` — endpoints/readiness JSONB round-trip fidelity |
| `tests/integration/test_jsonb_numeric_fidelity.py` | `CapabilityRepository`, `ProviderRepository`, `WorkloadRepository` — JSONB and NUMERIC(12,6) round-trip |
| `tests/integration/test_redis_and_dedup.py` | `WebhookDeliveryRepository` uniqueness constraint dedup behaviour |
| `tests/integration/test_reconcile_idempotency_concurrency.py` | `WorkloadRepository.insert` concurrency with idempotency key |
| `tests/integration/test_guarded_transition_concurrency.py` | `WorkloadRepository.guarded_transition` row-lock/concurrent-transition semantics |
| `tests/integration/test_lease_persist_loop.py` | Pre-readiness `LeaseRepository.create` callback on async event loop |
| `tests/integration/conftest.py` | Fixture that builds SQL from all `db/migrations/*.sql` via `discover_migrations`, applies it to a live `pg_pool` fixture |
| `tests/integration/test_lease_persist_loop.py` | `_make_pre_lease_persist_callback` → `LeaseRepository.create` integration |
| `tests/release/test_sovereignty_tier.py` | Mocks `CapabilityRepository` and `ProviderRepository` for tier integration tests |
| `tests/release/test_kill_drill_tier.py` | `persist_kill_report` integration — kill-log persistence after kill switch activation |
| `tests/release/test_dry_run_tier.py` | Mocks `CapabilityRepository` and `ProviderRepository` for dry-run tier |
| `tests/security/conftest.py` | Stubs `WebhookDeliveryRepository` to prevent route touching the real pool |
| `tests/perf/test_webhook_fast200.py` | Stubs `WebhookDeliveryRepository` for perf test isolation |
| `tests/chaos/test_serverless_5xx.py` | Verifies persistent 5xx path raises |
| `tests/chaos/test_serverless_429.py` | Verifies persistent 429 path raises eventually |

## 8. Dependencies

**Intra-pitwall imports:**

| Importing module | Uses from db/ |
|-----------------|---------------|
| `pitwall.api.app` | `db_lifespan` (FastAPI lifespan); `get_pool` called at module load to assert `DATABASE_URL` |
| `pitwall.webhook_receiver` | `DATABASE_URL` env var |
| `pitwall.workload_lifecycle` | `REDIS_URL` env var |
| `pitwall.reconciler` | `LeaseRepository`, `ProviderRepository`, workload/lease state queries; `REDIS_URL`, `RUNPOD_API_KEY`, `PITWALL_LEASE_ADVANCE_WARNING_MIN` |
| `pitwall.audit.capability` | `insert_audit`, `list_audit`; budget env vars |
| `pitwall.cost.budget_gate` | `PITWALL_MONTHLY_BUDGET_USD`, `PITWALL_PER_REQUEST_MAX_USD`, `PITWALL_BUDGET_LOCK_KEY` |
| `pitwall.cost.exporter` | `DATABASE_URL` |
| `pitwall.ops.backup_drill` | `PITWALL_DATABASE_URL` or `DATABASE_URL`; `Pool` for backup/restore drills |
| `pitwall.core.models` | *(data models consumed by repositories)* |
| `pitwall.api.leases.launch` | `LeaseRepository` via `_make_pre_lease_persist_callback` |

**External libraries:**

| Library | Usage |
|---------|-------|
| `asyncpg` | Connection pool (`asyncpg.Pool`), raw SQL execution, `set_type_codec` for JSONB, `asyncpg.Record` row type |
| `pydantic` v2 | Domain models (`Capability`, `Provider`, `Lease`, `Workload`, etc.) returned by all repository methods |
| `fastapi` | `Depends`, `Request`, `FastAPI`; `db_lifespan` context manager |
| Python standard library: `hashlib`, `dataclasses`, `pathlib`, `subprocess`, `json`, `datetime`, `os` | — |
