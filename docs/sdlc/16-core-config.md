# SDLC §16 — Core Models & Configuration

## 1. Purpose & Scope

The **Core Models & Configuration** subsystem is the foundation layer of the Pitwall GPU broker. It comprises:

- `src/pitwall/config.py` — strict Pydantic settings populated from environment variables and optional TOML config, boot-time domain-config validation, a fail-closed service bootstrapping gate, and a cached global `PitwallSettings` singleton.
- `src/pitwall/core/models.py` — Pydantic v2 domain objects (`Capability`, `Provider`, `Lease`, `Workload`, etc.) that form the persisted registry and runtime record vocabulary.
- `src/pitwall/core/enums.py` — `StrEnum` values for capability classes, lease/workload lifecycle states, provider types, cost modes, and registry prefixes.
- `src/pitwall/core/` leaf modules — `ids.py`, `idempotency.py`, `jobs.py`, `inference.py`, `cost_reporting.py`, `errors.py`, `types.py`, `constants.py`.

All other Pitwall subsystems (`api`, `routing`, `cost`, `runpod_client`, `db`, etc.) import domain models and settings from this subsystem. It has **no runtime dependencies on any other Pitwall package** — only standard-library, Pydantic, and `asyncpg`.

---

## 2. Components

### `src/pitwall/core/__init__.py`

Re-exports the public surface of `pitwall.core`. The module docstring states the design constraint: heavier modules (`inference`, `cost`, routing) are **not** re-exported here to avoid import cycles — they are imported by full path where needed.

**Public exports:**

| Symbol | Origin |
|---|---|
| `Capability`, `CapabilityDefaults`, `Provider`, `Lease`, `LeaseEndpoints`, `LeaseReadiness`, `LeaseTcpEndpoint`, `Workload`, `ConfigAuditEntry`, `RateBucket`, `WebhookSubscription*` | `core.models` |
| `CapabilityClass`, `CapabilityHint`, `CapabilitySource`, `CostMode`, `LeaseRenewalPolicy`, `LeaseState`, `ProviderType`, `RegistryPrefix`, `ResultDelivery`, `WorkloadState` | `core.enums` |
| `ULID`, `ulid_new()` | `core.ids` |
| `reserve_idempotency_key()`, `IdempotencyReservation`, `IdempotencyMismatch` | `core.idempotency` |
| `transition_workload()` | `core.jobs` |

### `src/pitwall/core/enums.py`

`StrEnum` definitions for all domain vocabulary. All enums use `str` base so serialized forms are plain strings.

| Enum | Values | File line |
|---|---|---|
| `RegistryPrefix` | `GHCR_IO`, `GITLAB_REGISTRY`, `DOCKER_HUB`, `DOCKER_HUB_ALT`; `from_image_ref()` classmethod | `:8–31` |
| `CapabilityClass` | `EMBEDDING`, `RERANK`, `LLM`, `VISION`, `TRANSCRIBE`, `GPU_LEASE`, `CUSTOM` | `:34–43` |
| `CostMode` | `PER_SECOND`, `PER_REQUEST`, `PER_TOKEN` | `:46–51` |
| `CapabilitySource` | `API`, `MCP`, `YAML` | `:54–59` |
| `ResultDelivery` | `SYNC`, `ASYNC` | `:62–66` |
| `CapabilityHint` | `LATENCY_SENSITIVE`, `COST_SENSITIVE`, `REGION_PREFERENCE` | `:69–74` |
| `WorkloadState` | `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT` | `:77–85` |
| `ProviderType` | `SERVERLESS_QUEUE`, `SERVERLESS_LB`, `PUBLIC_ENDPOINT`, `POD_LEASE` | `:88–94` |
| `LeaseState` | `CREATING`, `WAITING_RUNTIME`, `WAITING_PROBE`, `ACTIVE`, `STOPPING`, `STOPPED`, `FAILED`, `EXPIRED` | `:97–107` |
| `LeaseRenewalPolicy` | `MANUAL` (currently; other values defined in spec) | `:110–112` |

### `src/pitwall/core/models.py`

All models inherit from `PitwallModel` (`:64`), which enforces `extra="forbid"`, `populate_by_name=True`, `str_strip_whitespace=True`, and `use_enum_values=False`. Typed fields use `Strict*` wrappers (`:48–57`) that validate enum values arrive as strings before coercion, raising `ValueError` otherwise.

**Key types (non-model):**

- `UTCDateTime` (`:47`) — `datetime` with tzinfo required; rejects naive datetimes.
- `UsdAmount` (`:32`) — `Decimal` ge=0, max 12 digits, 6 decimal places.
- `NonEmptyString`, `NonNegativeInt` (`:60–61`).
- `JsonObject` (`:31`) — plain `dict[str, Any]`.

**Domain models:**

| Model | Responsibility | Key invariant | Line |
|---|---|---|---|
| `CapabilityDefaults` | Default execution settings attached to a capability | `execution_timeout_ms` default 60 000; `ttl_ms` default 300 000; `result_delivery` default `SYNC` | `:75–80` |
| `Capability` | What a consumer asks Pitwall to fulfill | `id`, `name`, `version`, `class_` required; `has_active_signals`-equivalent via `capability_class` property | `:83–108` |
| `Workload` | Persisted unit of consumer-requested work | `id`, `capability_id`, `provider_id`, `type`, `state`, `submitted_at` required; nullable timing/cost/error fields | `:111–135` |
| `LeaseTcpEndpoint` | TCP proxy endpoint exposed by RunPod for a lease | `port` range 1–65535 | `:138–142` |
| `LeaseEndpoints` | HTTP and TCP endpoints for a pod lease | Both fields default to empty dict | `:145–149` |
| `LeaseReadiness` | Readiness signals before a lease becomes active | `has_active_signals` property: all three timestamps (`runtime_seen_at`, `port_mappings_seen_at`, `probe_passed_at`) must be non-None | `:152–166` |
| `Lease` | Stateful RunPod pod allocation from create through teardown | Lifecycle validator: `expires_at > created_at`; `ACTIVE` requires `endpoints` and `readiness.has_active_signals` | `:169–198` |
| `Provider` | Concrete fulfillment binding to a RunPod resource | `id`, `capability_id`, `name`, `provider_type`, `updated_at` required; `priority >= 0`; `recent_error_rate` range 0–1 | `:204–227` |
| `ConfigAuditEntry` | A single row in the config mutation audit trail | `id` is `int` (not ULID); all value fields nullable | `:230–241` |
| `RateBucket` | Token-bucket state for RunPod endpoint rate limiting | `capacity > 0`; `tokens >= 0` | `:244–252` |
| `WebhookSubscription` | Consumer-registered webhook URL for async result callbacks | `hmac_secret` has `repr=False` to prevent accidental exposure | `:255–268` |
| `WebhookDeliveryFailure` | Failed webhook delivery attempt record | `attempt` range 1–4 (max 4 retries) | `:297–313` |

### `src/pitwall/core/ids.py`

ULID identifier helpers. ULIDs follow the pattern `{prefix}_{ulid}` in the application layer (e.g., `wkl_01ARYZ...`).

- `ULID_PATTERN` (`:14`) — `r"^[0-9A-HJKMNP-TV-Z]{26}$"`, excludes I, L, O, U per Crockford base32.
- `ULID` type alias (`:18–26`) — pydantic `Annotated[str, Field(min_length=26, max_length=26, pattern=ULID_PATTERN)]`.
- `ulid_new()` (`:30–38`) — lazily imports `python-ulid` to avoid import-time cost when only types are needed.
- `is_valid_ulid()` (`:41–45`) — pure Python validation without the library.

### `src/pitwall/core/idempotency.py`

Atomic idempotency-key reservation inside an `asyncpg` transaction. Uses `ON CONFLICT DO NOTHING` for insert, then a separate lookup + body-hash comparison to detect safe replays vs. mutation attempts.

- `reserve_idempotency_key(conn, *, key, body_hash, workload_id)` (`:61–94`) — inserts or looks up; raises `IdempotencyMismatch` if same key with different body hash.
- `_hash_input()` (`:36–41`) — SHA-256 of JSON-serialized input (null → `sha256(b"null")`).
- `IdempotencyMismatch` (`:26–33`) — exception with `original_workload_id` attribute.
- `IdempotencyReservation` (`:18–23`) — `dataclass` result with `is_new: bool` and `workload_id: str`.

### `src/pitwall/core/jobs.py`

Guarded workload state transitions. Single `UPDATE … WHERE id = $N AND state IN (…)` ensures atomicity within the caller's transaction.

- `transition_workload(conn, *, workload_id, from_states, to_state, patch)` (`:18–68`) — `True` iff exactly one row updated; `False` if workload not in `from_states`. JSONB columns (`input`, `result`, `error`) cast with `::jsonb`; others passed as-is.

### `src/pitwall/core/inference.py`

**Not re-exported from `pitwall.core`** to avoid the heavy cost/routing/runpod import graph. Imported by full path where needed.

- `resolve_inference_target()` (`:81–108`) — builds `RoutingRequest` and calls `resolve_capability()`.
- `run_sync_inference()` (`:134–178`) — budget gate admission + timed RunPod serverless-LB call.
- `create_and_dispatch_job()` (`:220–265`) — inserts QUEUED workload, dispatches to RunPod queue, updates `runpod_job_id`.
- `cancel_job()` (`:268–314`) — best-effort cancel; logs on RunPod failure but always returns an outcome.
- `record_inference_trace()` (`:181–217`) — emits Langfuse trace and persists `langfuse_trace_id` on the workload row.

### `src/pitwall/core/cost_reporting.py`

Read-only aggregation queries over `pitwall.workloads` and `pitwall.cost_daily`.

- `cost_summary(pool, *, capability_class, since, until)` (`:16–95`) — returns `{total_usd: float, entries: list[dict]}`.
- `recent_workloads(pool, *, capability_id, provider_id, provider_type, state, since, until, limit)` (`:98–226`) — returns `{workloads: list[dict]}` with float conversion on cost fields.

### `src/pitwall/core/errors.py`

Currently empty (`__all__ = []`). Shared error types are defined in-place in each module (`IdempotencyMismatch`, `R2TempCredentialError`, etc.).

### `src/pitwall/core/types.py` / `constants.py`

Both currently empty (`__all__ = []`). Reserved for future cross-package type aliases and shared constants.

---

## 3. Domain Model

### Enums (`core.enums`)

All enums inherit `StrEnum`. Serialization always produces the string value. `CapabilityHint` and `CapabilitySource` drive provider ranking and capability registration respectively. `RegistryPrefix.from_image_ref()` is the single dispatch point for selecting registry auth credentials based on Docker image ref prefix.

### PitwallModel Base (`core/models.py:64`)

`PitwallModel` enforces **strictness** across all child models:

```python
model_config = ConfigDict(
    extra="forbid",        # reject unknown fields
    populate_by_name=True,  # allow alias and field name
    str_strip_whitespace=True,
    use_enum_values=False, # serialize enum members as objects by default
)
```

### Capability / Provider

`Capability` (`:83`) describes what a consumer requests. `Provider` (`:204`) describes the RunPod binding that fulfils it. Both support `source` (`API`, `MCP`, `YAML`) and a `last_applied_yaml_hash` for YAML-registered capabilities. The `Provider.config` JSON field carries RunPod-specific template parameters (container disk GB, worker counts, image download commands, etc.).

### Lease / Workload

`Lease` (`:169`) models the stateful pod lifecycle from `CREATING` through `ACTIVE` to `STOPPED`/`EXPIRED`. Its model validator enforces `expires_at > created_at` and that `ACTIVE` leases have observed endpoints and three readiness signals.

`Workload` (`:111`) models individual inference jobs with full timing (queue, cold-start, execution) and cost tracking. `WorkloadState` transitions are driven atomically by `transition_workload()`.

### Settings Loading (`src/pitwall/config.py:835`)

`load_settings_from_env()` returns `PitwallSettings()`; the model customizes settings sources as init values, explicit environment variables, optional TOML config, then model defaults (`src/pitwall/config.py:191`, `src/pitwall/config.py:199`, `src/pitwall/config.py:201`, `src/pitwall/config.py:203`, `src/pitwall/config.py:204`, `src/pitwall/config.py:835`, `src/pitwall/config.py:844`). Environment values win over TOML file values because `_PitwallEnvSettingsSource` precedes `_PitwallTomlSettingsSource` in that tuple. The env source returns only explicitly configured env values, so field defaults do not mask file values (`src/pitwall/config.py:667`, `src/pitwall/config.py:670`).

The env source still uses private helpers:

- `_get_env(key, default)` — raw `os.environ.get`.
- `_get_bool_env(key, default)` — parses `1/true/yes/on` and `0/false/no/off`; raises `ValueError` on invalid string.
- `_get_first_env(*keys, default)` — returns first non-empty value across aliases.

### TOML Config File (`src/pitwall/config.py:557`)

`PITWALL_CONFIG_FILE` points at an explicit config path; if it is unset, `resolve_config_file()` uses `./pitwall.toml` only when that file exists (`src/pitwall/config.py:35`, `src/pitwall/config.py:36`, `src/pitwall/config.py:582`, `src/pitwall/config.py:589`, `src/pitwall/config.py:592`). An explicit missing file or non-`.toml` suffix raises `ConfigFileError`; an absent default `pitwall.toml` contributes no file settings (`src/pitwall/config.py:566`, `src/pitwall/config.py:567`, `src/pitwall/config.py:569`, `src/pitwall/config.py:571`). The file is loaded through `pydantic-settings` `TomlConfigSettingsSource(..., toml_file=path)` and normalized before merge (`src/pitwall/config.py:24`, `src/pitwall/config.py:575`, `src/pitwall/config.py:576`, `src/pitwall/config.py:579`); Pitwall declares `pydantic-settings` as a runtime dependency and requires Python >=3.12 (`pyproject.toml:11`, `pyproject.toml:26`).

Accepted TOML keys mirror `PitwallSettings` field names plus env-style aliases: `_config_file_key_to_field()` accepts each field name, its uppercase form, and every alias from `_ENV_FIELD_ALIASES` (`src/pitwall/config.py:444`, `src/pitwall/config.py:596`, `src/pitwall/config.py:604`, `src/pitwall/config.py:606`, `src/pitwall/config.py:608`, `src/pitwall/config.py:609`, `src/pitwall/config.py:611`). Entrypoint-only bind host env vars are not `PitwallSettings` fields; see Configuration.

### Fail-Closed Boot (`src/pitwall/config.py:937`)

`require_runtime_env(service)` is called by every Pitwall service entrypoint before accepting traffic. It:

1. Calls `check_domain_config(service)`, which normalizes the service name, loads settings if needed, and runs the boot-time domain-config checks (`src/pitwall/config.py:899`, `src/pitwall/config.py:906`, `src/pitwall/config.py:907`, `src/pitwall/config.py:909`, `src/pitwall/config.py:914`).
2. Converts `ConfigFileError`, `ValidationError`, and parsing `ValueError` into sanitized stderr text and raises `SystemExit(os.EX_CONFIG)` (`src/pitwall/config.py:944`, `src/pitwall/config.py:946`, `src/pitwall/config.py:947`, `src/pitwall/config.py:948`, `src/pitwall/config.py:1264`, `src/pitwall/config.py:1266`, `src/pitwall/config.py:1271`).
3. Prints warnings and continues; prints errors and exits `os.EX_CONFIG` (`src/pitwall/config.py:949`, `src/pitwall/config.py:950`, `src/pitwall/config.py:952`, `src/pitwall/config.py:955`).

Default required env for unknown services: `RUNPOD_API_KEY`, `DATABASE_URL`, `REDIS_URL` (`src/pitwall/config.py:893`, `src/pitwall/config.py:896`).

### Boot-Time Domain-Config Checks (`src/pitwall/config.py:899`)

| Check | Inputs | Failure mode |
|---|---|---|
| Required runtime settings | Per-service set from `_REQUIRED_ENV_BY_SERVICE`; missing and whitespace-only values fail (`src/pitwall/config.py:847`, `src/pitwall/config.py:849`, `src/pitwall/config.py:855`, `src/pitwall/config.py:958`, `src/pitwall/config.py:964`, `src/pitwall/config.py:966`, `src/pitwall/config.py:972`) | error -> `SystemExit(os.EX_CONFIG)` |
| Budget settings | `PITWALL_MONTHLY_BUDGET_USD`, `PITWALL_PER_REQUEST_MAX_USD` (`src/pitwall/config.py:986`, `src/pitwall/config.py:987`, `src/pitwall/config.py:995`, `src/pitwall/config.py:1003`) | negative values are errors; per-request cap above monthly cap is a warning |
| Timeout and port settings | Lease/audit timeout settings plus `PITWALL_WEBHOOK_RECEIVER_PORT` and `PITWALL_COST_EXPORTER_PORT` (`src/pitwall/config.py:1019`, `src/pitwall/config.py:1020`, `src/pitwall/config.py:1039`, `src/pitwall/config.py:1040`, `src/pitwall/config.py:1041`, `src/pitwall/config.py:1052`, `src/pitwall/config.py:1224`) | non-positive timeout, invalid port, or audit max below exec timeout is an error; image-pull timeout below startup timeout is a warning |
| Embedding URL | `PITWALL_BASE_URL` when `PITWALL_EMBEDDING_VIA_PITWALL=true` (`src/pitwall/config.py:1065`, `src/pitwall/config.py:1066`, `src/pitwall/config.py:1071`) | missing base URL is an error |
| R2 temp credentials | `R2_TEMP_CREDENTIALS_ENABLED`, temp credential TTL, and required R2/Cloudflare fields (`src/pitwall/config.py:1078`, `src/pitwall/config.py:1079`, `src/pitwall/config.py:1080`, `src/pitwall/config.py:1089`, `src/pitwall/config.py:1097`, `src/pitwall/config.py:1109`, `src/pitwall/config.py:1124`, `src/pitwall/config.py:1135`) | invalid mode/TTL or required missing fields are errors; partial `auto` config is a warning |
| Optional integration groups | Langfuse, Resend alerts, and Tailscale/webhook pairing (`src/pitwall/config.py:1174`, `src/pitwall/config.py:1178`, `src/pitwall/config.py:1188`, `src/pitwall/config.py:1198`) | partial config is a warning |

### Cached Settings Singleton (`src/pitwall/config.py:1296`)

`@lru_cache(maxsize=1)` on `get_settings()` ensures the same `PitwallSettings` object is returned on every call within a process (`src/pitwall/config.py:1296`, `src/pitwall/config.py:1297`, `src/pitwall/config.py:1302`).

---

## 4. Public Interfaces

| Function / Class | Module | Signature |
|---|---|---|
| `PitwallSettings` | `config` | `BaseModel` subclass; `extra="forbid"` |
| `load_settings_from_env()` | `config` | `() -> PitwallSettings` |
| `get_settings()` | `config` | `() -> PitwallSettings` (cached) |
| `resolve_config_file()` | `config` | `(environ: dict[str, str] \| None = None, cwd: Path \| None = None) -> Path \| None` |
| `check_domain_config()` | `config` | `(service: str = "api", *, settings: PitwallSettings \| None = None) -> ConfigCheckResult` |
| `format_config_check_result()` | `config` | `(result: ConfigCheckResult) -> str` |
| `format_settings_load_error()` | `config` | `(exc: ConfigFileError \| ValidationError \| ValueError) -> str` |
| `require_runtime_env(service)` | `config` | `(service: str) -> None`; raises `SystemExit` |
| `required_runtime_env_vars(service)` | `config` | `(service: str) -> tuple[str, ...]` |
| `Capability`, `Provider`, `Lease`, `Workload`, etc. | `core/models` | `PitwallModel` subclasses |
| `ULID` type alias | `core/ids` | `Annotated[str, Field(...)]` |
| `ulid_new()` | `core/ids` | `() -> str` |
| `is_valid_ulid(value)` | `core/ids` | `(value: str) -> bool` |
| `reserve_idempotency_key()` | `core/idempotency` | `(conn, *, key, body_hash, workload_id) -> IdempotencyReservation` |
| `transition_workload()` | `core/jobs` | `(conn, *, workload_id, from_states, to_state, patch=None) -> bool` |
| `RegistryPrefix.from_image_ref()` | `core/enums` | `(image_ref: str) -> RegistryPrefix \| None` |
| `resolve_inference_target()` | `core/inference` | async; full sig in `:81` |
| `run_sync_inference()` | `core/inference` | async; full sig in `:134` |
| `create_and_dispatch_job()` | `core/inference` | async; full sig in `:220` |
| `cancel_job()` | `core/inference` | async; full sig in `:268` |
| `cost_summary()` | `core/cost_reporting` | async; full sig in `:16` |
| `recent_workloads()` | `core/cost_reporting` | async; full sig in `:98` |

---

## 5. Configuration

All environment variables consumed by this subsystem. `PitwallSettings` fields live in `src/pitwall/config.py:207` through `src/pitwall/config.py:437`; env aliases live in `_ENV_FIELD_ALIASES` (`src/pitwall/config.py:444`, `src/pitwall/config.py:545`). Entrypoint-only bind hosts are listed separately below.

### Loader-only

| Env var | Pydantic field | Default |
|---|---|---|
| `PITWALL_CONFIG_FILE` | *(loader-only)* | unset; falls back to `./pitwall.toml` only if it exists (`src/pitwall/config.py:35`, `src/pitwall/config.py:36`, `src/pitwall/config.py:589`, `src/pitwall/config.py:592`) |

### Required at boot (fail-closed if missing)

| Env var | Pydantic field | Default |
|---|---|---|
| `RUNPOD_API_KEY` | `runpod_api_key` | *(none)* |
| `DATABASE_URL` | `database_url` | *(none)* |
| `REDIS_URL` | `redis_url` | *(none)* |

### RunPod

| Env var | Pydantic field | Default |
|---|---|---|
| `RUNPOD_REST_API_URL` | `runpod_rest_api_url` | `https://rest.runpod.io/v1` |
| `RUNPOD_NETWORK_VOLUME_ID` | `runpod_network_volume_id` | `""` |
| `RUNPOD_DATA_CENTER_ID` | `runpod_data_center_id` | `""` |
| `RUNPOD_REGISTRY_AUTH_ID` | `runpod_registry_auth_id` | `""` |
| `RUNPOD_REGISTRY_AUTH_ID_GHCR` | `runpod_registry_auth_id_ghcr` | `""` |
| `RUNPOD_REGISTRY_AUTH_ID_GITLAB` | `runpod_registry_auth_id_gitlab` | `""` |
| `RUNPOD_REGISTRY_AUTH_ID_DOCKER_HUB` | `runpod_registry_auth_id_docker_hub` | `None` |

### R2 / Cloudflare

| Env var(s) | Pydantic field | Default |
|---|---|---|
| `R2_ENDPOINT` | `r2_endpoint` | `""` |
| `R2_ACCESS_KEY` | `r2_access_key` | `""` |
| `R2_SECRET_KEY` | `r2_secret_key` | `""` |
| `R2_PARENT_ACCESS_KEY_ID` / `CLOUDFLARE_R2_PARENT_ACCESS_KEY_ID` / `R2_ACCESS_KEY_ID` / `R2_ACCESS_KEY` | `r2_parent_access_key_id` | `""` |
| `R2_BUCKET_STAGING` | `r2_bucket_staging` | `pitwall-staging` |
| `R2_TEMP_CREDENTIALS_ENABLED` / `PITWALL_R2_TEMP_CREDENTIALS_ENABLED` | `r2_temp_credentials_enabled` | `"auto"` |
| `R2_TEMP_CREDENTIALS_REQUIRED` / `PITWALL_R2_TEMP_CREDENTIALS_REQUIRED` | `r2_temp_credentials_required` | `False` |
| `R2_TEMP_CREDENTIAL_TTL_S` + 7 aliases | `r2_temp_credential_ttl_s` | `21_600` |
| `R2_TEMP_CREDENTIAL_PERMISSION` + 3 aliases | `r2_temp_credential_permission` | `"object-read-write"` |
| `R2_TEMP_CREDENTIAL_PREFIXES` / `PITWALL_R2_TEMP_CREDENTIAL_PREFIXES` | `r2_temp_credential_prefixes` | `""` |
| `R2_TEMP_CREDENTIAL_OBJECTS` / `PITWALL_R2_TEMP_CREDENTIAL_OBJECTS` | `r2_temp_credential_objects` | `""` |
| `CLOUDFLARE_ACCOUNT_ID` / `CF_ACCOUNT_ID` / `R2_ACCOUNT_ID` | `cloudflare_account_id` | `""` |
| `CLOUDFLARE_API_TOKEN` / `CF_API_TOKEN` / `R2_TEMP_CREDENTIAL_API_TOKEN` | `cloudflare_api_token` | `""` |

### Budget

| Env var | Pydantic field | Default |
|---|---|---|
| `PITWALL_MONTHLY_BUDGET_USD` | `pitwall_monthly_budget_usd` | `50.0` |
| `PITWALL_PER_REQUEST_MAX_USD` | `pitwall_per_request_max_usd` | `10.0` |
| `PITWALL_BUDGET_LOCK_KEY` | `pitwall_budget_lock_key` | `5494545452575544` |

### Lease / timeouts

| Env var | Pydantic field | Default |
|---|---|---|
| `PITWALL_DEFAULT_LEASE_TTL_S` | `pitwall_default_lease_ttl_s` | `7200` |
| `PITWALL_LEASE_ADVANCE_WARNING_MIN` | `pitwall_lease_advance_warning_min` | `"15,5"` |
| `PITWALL_VOLUME_ATTACH_TIMEOUT_S` | `pitwall_volume_attach_timeout_s` | `300` |
| `PITWALL_IMAGE_PULL_TIMEOUT_S` | `pitwall_image_pull_timeout_s` | `600` |

### Audit

| Env var | Pydantic field | Default |
|---|---|---|
| `PITWALL_AUDIT_GPU_IDS` | `pitwall_audit_gpu_ids` | `"NVIDIA H100 80GB HBM3,NVIDIA L4,NVIDIA A100 80GB"` |
| `PITWALL_AUDIT_CLOUD_TYPE` | `pitwall_audit_cloud_type` | `"SECURE"` |
| `PITWALL_AUDIT_EXEC_TIMEOUT_S` | `pitwall_audit_exec_timeout_s` | `3600` |
| `PITWALL_AUDIT_EXEC_TIMEOUT_MAX_S` | `pitwall_audit_exec_timeout_max_s` | `7200` |
| `PITWALL_AUDIT_QUEUE_TIME_S` | `pitwall_audit_queue_time_s` | `300` |
| `PITWALL_AUDIT_STARTUP_TIMEOUT_S` | `pitwall_audit_startup_timeout_s` | `600` |

### Worker / operator

| Env var | Pydantic field | Default |
|---|---|---|
| `PITWALL_CLOUD_WORKER_IMAGE` | `pitwall_cloud_worker_image` | `""` |
| `PITWALL_RUNPOD_CAPACITY_ERROR_SUBSTRINGS` | `pitwall_gpu_broker_capacity_error_substrings` | `""` |
| `PITWALL_EMBEDDING_VIA_PITWALL` | `pitwall_embedding_via_pitwall` | `False` |
| `PITWALL_BASE_URL` | `pitwall_base_url` | `""` |

### Bind hosts (entrypoint-only)

These host settings are direct `os.environ` reads in the bare `python -m ...` entrypoints, not `PitwallSettings` fields. Their loopback defaults support the inbound trust-boundary posture described in `14-security.md` (`docs/sdlc/14-security.md:3`, `docs/sdlc/14-security.md:4`, `docs/sdlc/14-security.md:9`, `docs/sdlc/14-security.md:18`).

| Env var | Pydantic field | Default |
|---|---|---|
| `PITWALL_API_HOST` | *(entrypoint-only)* | `127.0.0.1` (`src/pitwall/api/__main__.py:11`) |
| `PITWALL_WEBHOOK_HOST` | *(entrypoint-only)* | `127.0.0.1` (`src/pitwall/webhook_receiver/__main__.py:11`) |
| `PITWALL_COST_EXPORTER_HOST` | *(entrypoint-only)* | `127.0.0.1` (`src/pitwall/cost_exporter/__main__.py:11`; console entrypoint at `pyproject.toml:52`) |

### Observability / webhooks

| Env var | Pydantic field | Default |
|---|---|---|
| `LANGFUSE_HOST` | `langfuse_host` | `""` |
| `LANGFUSE_PUBLIC_KEY` | `langfuse_public_key` | `""` |
| `LANGFUSE_SECRET_KEY` | `langfuse_secret_key` | `""` |
| `PITWALL_TAILSCALE_IP` | `pitwall_tailscale_ip` | `""` |
| `PITWALL_WEBHOOK_PUBLIC_URL` | `pitwall_webhook_public_url` | `""` |
| `PITWALL_WEBHOOK_RECEIVER_PORT` | `pitwall_webhook_receiver_port` | `8082` |
| `PITWALL_MCP_TRANSPORT` | `pitwall_mcp_transport` | `"stdio"` |
| `PITWALL_COST_EXPORTER_PORT` | `pitwall_cost_exporter_port` | `9109` |
| `PITWALL_ALERT_FROM` | `pitwall_alert_from` | `""` |
| `PITWALL_ALERT_TO` | `pitwall_alert_to` | `""` |
| `RESEND_API_KEY` | `resend_api_key` | `""` |
| `RESEND_SENDER_EMAIL` | `resend_sender_email` | `""` |
| `RESEND_BUDGET_ALERT_EMAIL` | `resend_budget_alert_email` | `""` |
| `PITWALL_ADMIN_SECRET` | `pitwall_admin_secret` | `""` |
| `PITWALL_API_TOKEN` | `pitwall_api_token` | `""` |
| `PITWALL_INBOUND_RATE_LIMIT` | `pitwall_inbound_rate_limit` | `""` |

---

## 6. Failure Modes & Error Types

### `SystemExit(os.EX_CONFIG)` — invalid boot config

`require_runtime_env()` catches settings load errors, prints sanitized text, and raises `SystemExit(os.EX_CONFIG)`; after a successful load, it prints warnings but exits only when `check_domain_config()` returns errors (`src/pitwall/config.py:944`, `src/pitwall/config.py:946`, `src/pitwall/config.py:947`, `src/pitwall/config.py:948`, `src/pitwall/config.py:949`, `src/pitwall/config.py:952`, `src/pitwall/config.py:955`).

Affected required-runtime-env sets: `api`, `reconciler`, `worker`, `mcp` require `RUNPOD_API_KEY + DATABASE_URL + REDIS_URL`; `cost-exporter` requires only `DATABASE_URL`; `webhook` has no required env (`src/pitwall/config.py:847`, `src/pitwall/config.py:849`, `src/pitwall/config.py:855`). The broader domain-config errors above apply to every service.

### `ConfigFileError` — invalid TOML config file

Raised when an explicit `PITWALL_CONFIG_FILE` is missing, a discovered config path is not `.toml`, or `TomlConfigSettingsSource` cannot read/parse the file; `require_runtime_env()` maps it to `SystemExit(os.EX_CONFIG)` (`src/pitwall/config.py:440`, `src/pitwall/config.py:566`, `src/pitwall/config.py:567`, `src/pitwall/config.py:571`, `src/pitwall/config.py:575`, `src/pitwall/config.py:576`, `src/pitwall/config.py:577`, `src/pitwall/config.py:946`, `src/pitwall/config.py:948`).

### `ValueError` — invalid boolean or enum env parsing

`_get_bool_env()` raises `ValueError` if a non-boolean string is assigned to a boolean-typed env var (`src/pitwall/config.py:634`, `src/pitwall/config.py:642`, `src/pitwall/config.py:647`).

`Strict*` validators in `core/models.py` raise `ValueError` if enum values are not strings (`:35–38`).

### `pydantic.ValidationError` — invalid env value type

Pydantic itself raises `ValidationError` when an env var cannot be coerced to the declared type (e.g., non-numeric string for an `int` field).

### `IdempotencyMismatch` (`core/idempotency.py:26`)

Raised when an idempotency key is replayed with a different request body hash. Carries `original_workload_id`. API layer maps this to HTTP 422.

### Lease lifecycle validator (`core/models.py:187`)

`ValueError` raised during `.model_validate()` if:
- `expires_at <= created_at`
- State is `ACTIVE` but `endpoints` is `None`
- State is `ACTIVE` but readiness signals are incomplete

### R2 temp credential errors (`r2_temp_credentials.py`)

`R2TempCredentialConfigError` — TTL out of range, missing required fields in `required` mode, invalid permission literal.

`R2TempCredentialError` — HTTP errors, non-success Cloudflare responses, missing required result fields, malformed JSON.

---

## 7. GitOps Desired-State Governance

`src/pitwall/gitops/` reconciles capability and provider registry configuration from versioned YAML. The required root version is `apiVersion: pitwall.dev/v1`; each document can declare `capabilities` and `providers`. The loader reuses the seed-file YAML/JSON parser for hermetic operation, then validates a stricter GitOps schema (`schema.py`). Unknown fields fail closed through the shared `PitwallModel` policy.

### Plan model

`build_reconcile_plan()` (`differ.py`) compares desired YAML against live `Capability` and `Provider` models and returns a deterministic `ReconcilePlan`. Operations are structured as `create`, `update`, or `delete`, keyed by `capability` or `provider`, with field-level `changes`, old/current snapshots, desired snapshots, and a `destructive` flag.

GitOps only plans delete operations for YAML-owned live rows: rows with `source == YAML` or a non-null `last_applied_yaml_hash`. API/MCP-owned rows outside the desired files are left alone. Desired rows with the same name as an existing live row adopt the existing ID unless the YAML explicitly declares a conflicting ID, which fails before apply.

### Apply gate

`apply_plan()` (`reconcile.py`) defaults to dry-run and performs no repository writes or audit writes unless `dry_run=False` is supplied. Plans containing deletes require `allow_delete=True`; otherwise apply raises `GitOpsDestructiveChangeError`.

Applied creates and updates go through the existing repository upsert methods, preserving provider runtime health/cooldown telemetry on provider updates. Delete operations are soft-deletes through `disable()`, not physical row removal. Every non-dry-run operation writes `config_audit` through `insert_audit` with actor default `gitops:admin`, action `gitops:create`, `gitops:update`, or `gitops:delete`, and the structured old/new snapshots.

### Example

```yaml
apiVersion: pitwall.dev/v1
capabilities:
  - name: embedding.demo
    class: embedding
    cost_mode: per_second
    input_schema: {"type": "object"}
    output_schema: {"type": "object"}
providers:
  - name: embedding-demo-lb
    capability: embedding.demo
    provider_type: serverless_lb
    runpod_endpoint_id: eptest00000000
    region: US-KS-2
    priority: 10
    config:
      lb_base_url: https://eptest00000000.api.runpod.ai
```

---

## 8. Testing

Tests directly targeting this subsystem:

| File | What it covers |
|---|---|
| `tests/test_config_runtime_env.py` | `require_runtime_env()` / `required_runtime_env_vars()`; TOML default and `PITWALL_CONFIG_FILE`; env-over-file precedence; `check_domain_config()` errors; missing env -> `EX_CONFIG`; whitespace -> missing; service-name normalization; `cost-exporter` reduced env set; `mcp` full env set |
| `tests/gitops/test_reconcile_plan.py` | Versioned desired-state loading; deterministic create/update/delete plans; YAML-owned delete filtering; dry-run default; destructive apply gate; audit-backed apply path preserving provider runtime telemetry |
| `tests/property/test_gitops_properties.py` | Property coverage that create-plan ordering remains deterministic for any generated YAML order |
| `tests/test_r2_temp_credentials.py` | `R2TempCredentialEnvConfig.from_env()`; TTL validation (zero, negative, excessive, max-ok); prefix scoping; HTTP errors; non-success responses; `mint_r2_temp_credentials()` |
| `tests/test_smoke_packages.py:57–98` | `require_runtime_env` and `required_runtime_env_vars` exports present |
| `tests/cost/test_budget_gate.py:75` | Budget config rejects non-positive values |
| `tests/reconciler/test_init_coverage.py:37–55` | Redis config missing/invalid/valid paths |
| `tests/runpod_client/test_pods.py:53` | `capacity_error_substrings` env var override |
| `tests/leases/test_lease_landmines.py:341` | Provider config used in MCP lease pod mount path |
| `tests/db/test_config_audit_migration.py` | `config_audit` schema migration correctness |
| `tests/db/test_repository.py:291,496,744` | JSONB `config` round-trip patching on capabilities/providers |
| `tests/audit/test_runtime_config.py` | `RuntimeAuditConfig` (audit CLI wrapper over `AuditConfig` + env); all defaults, env overrides, protocol method presence |

Tests that **exercise but do not exclusively target** the subsystem (consumers of `PitwallSettings` or domain models):

- `tests/api/test_openai_proxy.py` / `test_openai_proxy_routes.py` — model serialization in API responses
- `tests/routing/test_*.py` — `Provider`/`Capability` routing logic
- `tests/leases/test_launch.py` — provider config in launch template
- `tests/cost/test_estimator.py` — cost config from provider `config` field

---

## 9. Dependencies

### Internal Pitwall packages

| Importing module | Imports from |
|---|---|
| `core/inference.py` | `pitwall.config` (`PitwallSettings`), `pitwall.core.enums`, `pitwall.core.ids`, `pitwall.core.jobs`, `pitwall.core.models`, `pitwall.cost.*`, `pitwall.db.repository`, `pitwall.observability.langfuse`, `pitwall.resolver`, `pitwall.routing`, `pitwall.runpod_client.*` |
| `core/jobs.py` | `asyncpg` only |
| `core/idempotency.py` | `asyncpg` only |
| `config.py` | `pitwall.r2_temp_credentials` (`R2TempCredentialPermission`, `DEFAULT_R2_TEMP_CREDENTIAL_TTL_S`, `_validate_permission`) |
| `core/models.py` | `pitwall.core.enums` |
| `core/__init__.py` | `core.enums`, `core.idempotency`, `core.ids`, `core.jobs`, `core.models` |
| `models.py` (public) | `pitwall.core.enums`, `pitwall.core.models` |

### External dependencies

| Library | Where used |
|---|---|
| `pydantic` | All models (`BaseModel`, `Field`, `ConfigDict`, `AfterValidator`, `BeforeValidator`, `model_validator`, `AliasChoices`) |
| `pydantic-settings` | `config.py` (`BaseSettings`, custom settings sources, `TomlConfigSettingsSource`); direct runtime dependency in `pyproject.toml:26` |
| `asyncpg` | `core/idempotency.py`, `core/jobs.py`, `core/inference.py`, `core/cost_reporting.py` |
| `python-ulid` | `core/ids.py:ulid_new()` — lazy import |
| `httpx` | `core/inference.py` (RunPod serverless-LB client), `r2_temp_credentials.py` |
| `pydantic` (second-party re-export) | `pitwall.r2_temp_credentials` re-exports from `config.py` |

### `pitwall.r2_temp_credentials` (separate package)

Imported by `config.py` to pull in `R2TempCredentialPermission` type, defaults, and the `_validate_permission` helper. This is the only external package in `src/pitwall/` that is not stdlib, pydantic, or asyncpg.
