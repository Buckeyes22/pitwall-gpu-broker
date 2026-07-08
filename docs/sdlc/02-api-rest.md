# REST API Surface — SDLC Subsystem Documentation

> All claims grounded in `src/pitwall/api/` source, with `path:line` citations.

## 1. Purpose & Scope

The Pitwall REST API (`src/pitwall/api/`) is the sole HTTP interface to the GPU broker.
FastAPI app at `/v1` for core routes, `/v1/admin` for administrative writes.

**What it does:** capability and provider registry CRUD, lease lifecycle (launch/renew/teardown),
sync inference dispatch with idempotency and budget gating, OpenAI-compatible passthrough proxy
with fallback chain, job status/result reads, webhook subscription registration,
admin pre-spend audit, and emergency kill-switch.

**Auth:** Non-health paths use opaque bearer authorization when
`PITWALL_API_TOKEN` or `PITWALL_API_SCOPED_TOKENS` is configured. Required
scopes are `read`, `spend`, `lease:mutate`, `webhook:admin`, or
`server:admin`. `/v1/admin/*` routes additionally require
`X-Pitwall-Secret`; without `PITWALL_ADMIN_SECRET` they fail closed with 401.
Non-loopback API startup requires both the all-scopes API token and admin secret.

**Position:** Top-most layer. Depends on `pitwall.db`, `pitwall.core`, `pitwall.cost`,
`pitwall.resolver`, `pitwall.routing`, `pitwall.runpod_client`, `pitwall.observability`.

---

## 2. Components

### `app.py` — Bootstrap and middleware (`src/pitwall/api/app.py`)

Fail-closed: `require_runtime_env("api")` runs at import; the required
`RUNPOD_API_KEY`, `DATABASE_URL`, and `REDIS_URL` globals are read immediately
after that (`src/pitwall/api/app.py:41`, `src/pitwall/api/app.py:43`,
`src/pitwall/api/app.py:44`, `src/pitwall/api/app.py:45`).

```
AdminSecretMiddleware(app, secret: str | None)
    async __call__(scope, receive, send)
        Gates /v1/admin and /v1/admin/* (src/pitwall/api/app.py:160).
        If secret is unset, returns 401
        {"detail": "admin routes disabled: PITWALL_ADMIN_SECRET is not configured"}
        (src/pitwall/api/app.py:162, src/pitwall/api/app.py:164,
        src/pitwall/api/app.py:167).
        If secret is set, compares X-Pitwall-Secret with hmac.compare_digest;
        mismatch returns 401 {"detail": "invalid or missing X-Pitwall-Secret"}
        (src/pitwall/api/app.py:176, src/pitwall/api/app.py:177,
        src/pitwall/api/app.py:179, src/pitwall/api/app.py:180).

ApiTokenMiddleware(app, token: str | None)
    If token is unset, passes through. If set, checks Authorization: Bearer
    <token> on every non-public-health path and returns 401
    {"detail": "invalid or missing bearer token"} plus WWW-Authenticate: Bearer
    on missing/invalid token (src/pitwall/api/app.py:87,
    src/pitwall/api/app.py:89, src/pitwall/api/app.py:91,
    src/pitwall/api/app.py:193, src/pitwall/api/app.py:195,
    src/pitwall/api/app.py:197, src/pitwall/api/app.py:199,
    src/pitwall/api/app.py:200, src/pitwall/api/app.py:201).

InboundRateLimitMiddleware(app, config: InboundRateLimitConfig | None, api_token)
    If config is unset, passes through. Public health/probe paths pass through.
    When the limit is exceeded, returns 429 {"detail": "rate limit exceeded"}
    with Retry-After (src/pitwall/api/app.py:221, src/pitwall/api/app.py:222,
    src/pitwall/api/app.py:227, src/pitwall/api/app.py:238,
    src/pitwall/api/app.py:240, src/pitwall/api/app.py:241,
    src/pitwall/api/app.py:242).

app = FastAPI(title="Pitwall API", version="1", lifespan=db_lifespan)
    Middleware is always installed:
    AdminSecretMiddleware (src/pitwall/api/app.py:290),
    ApiTokenMiddleware (src/pitwall/api/app.py:291),
    InboundRateLimitMiddleware (src/pitwall/api/app.py:292).
    Includes 9 routers (src/pitwall/api/app.py:313, src/pitwall/api/app.py:321).
    Exception handlers: PitwallApiError (src/pitwall/api/app.py:324),
    BudgetRejected (src/pitwall/api/app.py:329).

v1_health(request) -> dict[str, Any]  (src/pitwall/api/app.py:373)
    Checks postgres (SELECT 1 via pool) and redis (ping) →
    {"ok": bool, "postgres": {...}, "redis": {...}}.
```

Admin enabled/disabled log lines report configuration state only; the admin
middleware is installed either way (`src/pitwall/api/app.py:298`,
`src/pitwall/api/app.py:301`).

Public health/probe path exemptions are `/health`, `/healthz`, `/metrics`, `/ready`,
`/readiness`, `/readyz`, and `/v1/health` (`src/pitwall/api/app.py:52`,
`src/pitwall/api/app.py:53`, `src/pitwall/api/app.py:54`,
`src/pitwall/api/app.py:55`, `src/pitwall/api/app.py:56`,
`src/pitwall/api/app.py:57`, `src/pitwall/api/app.py:58`). `/healthz` and `/health` return
`{"ok": True, "backend": "runpod"}` (`src/pitwall/api/app.py:334`,
`src/pitwall/api/app.py:339`).

Request-time middleware order (outermost → innermost → routes):

1. `InboundRateLimitMiddleware` (`src/pitwall/api/app.py:292`)
2. `ApiTokenMiddleware` (`src/pitwall/api/app.py:291`)
3. `AdminSecretMiddleware` (`src/pitwall/api/app.py:290`)
4. Route handlers (`src/pitwall/api/app.py:313`, `src/pitwall/api/app.py:321`)

Inbound rate limiting is an opt-in REST-edge guard configured by
`PITWALL_INBOUND_RATE_LIMIT`; the middleware returns caller-visible 429 +
`Retry-After` when active and exhausted (`src/pitwall/api/app.py:137`,
`src/pitwall/api/app.py:149`, `src/pitwall/api/app.py:238`,
`src/pitwall/api/app.py:242`). Token-bucket details stay in
`11-rate-limiting.md`.

---

### `exceptions.py` — Mapped exception hierarchy (`src/pitwall/api/exceptions.py`)

14 exception classes, all inherit `PitwallApiError(status_code=500, error_code="internal_error")`.
`to_response_body()` → `{"error": "<code>", ...extra}`.

| Exception | status_code | error_code | Extra fields |
|---|---|---|---|
| `CapabilityNotFound` | 404 | `capability_not_found` | `name` |
| `CapabilityDisabled` | 409 | `capability_disabled` | `name` |
| `CapabilityConflict` | 409 | `capability_conflict` | `name` |
| `ProviderNotFound` | 404 | `provider_not_found` | `id` |
| `ProviderUnavailable` | 503 | `no_providers_available` | `capability`, `chain` |
| `ProviderConflict` | 409 | `provider_conflict` | `name` |
| `RateLimited` | 503 | `rate_limited` | `retry_after_s` |
| `LeaseNotFound` | 404 | `lease_not_found` | `id` |
| `LeaseStateConflict` | 409 | `lease_state_conflict` | `id`, `state`, `operation` |
| `ChangeSetTooBroad` | 400 | `change_set_too_broad` | `conflicting_fields` |
| `IdempotencyMismatch` | 422 | `idempotency_mismatch` | `original_workload_id` |
| `WorkloadNotFound` | 404 | `workload_not_found` | `id` |
| `JobNotReady` | 409 | `job_not_ready` | `id`, `state` |

---

### `capability_routes.py` (`src/pitwall/api/capability_routes.py`)

Admin: `POST /v1/admin/capabilities` (create, 201), `PATCH /v1/admin/capabilities/{id}`
(allows description/input_schema/output_schema/defaults/hints; blocks name/version/class_/cost_mode),
`POST /v1/admin/capabilities/{id}/enable`, `POST /v1/admin/capabilities/{id}/disable`.

Public: `GET /v1/capabilities` (list, uses `CapabilityListFilter`), `GET /v1/capabilities/{name}`
(lookup by name, not id).

All mutating handlers call `insert_audit(pool, actor="rest:admin", ...)`.

---

### `provider_routes.py` (`src/pitwall/api/provider_routes.py`)

Admin: `POST /v1/admin/providers` (201), `PATCH /v1/admin/providers/{id}`,
`POST /v1/admin/providers/{id}/enable`, `POST /v1/admin/providers/{id}/disable`,
`POST /v1/admin/providers/{id}/hibernate` (sets health_status="hibernated").

Public: `GET /v1/providers` (list), `GET /v1/providers/{id}`, `GET /v1/providers/{id}/health`
(health_status, cooldown_until, consecutive_failures, cooldown_trips, recent_error_rate).

`validate_provider_registration_config()` called on every create and every patch that
includes provider_type/endpoint_id/cloud_type/config — raises `HTTPException(422)` on
ValueError (provider_routes.py:171-181).

---

### `routes/leases.py` (`src/pitwall/api/routes/leases.py`)

`POST /v1/leases` — resolves capability, optionally resolves provider, calls
`run_launch()` (from `leases/launch.py`); dry_run returns plan without persisting.

`GET /v1/leases/{id}`, `PATCH /v1/leases/{id}` (validates multi-axis via
`lease_patch_conflicting_fields()` → `ChangeSetTooBroad` 400; supports only
`renewal_policy`, `auto_teardown_on_expiry`, and an optional idempotency key),
`POST /v1/leases/{id}/renew` (adds 1–43,200 minutes to the currently persisted
expiry, capped at 30 days from database time),
`POST /v1/leases/{id}/stop` (calls `run_teardown()`), `DELETE /v1/leases/{id}` (204, idempotent).

PATCH and renewal delegate to the shared `pitwall.leases.mutations` service.
They lock the lease row, write the mutation and audit record in one transaction,
and deduplicate retries when `idempotency_key` is supplied. Reusing a key with a
different operation or payload returns `idempotency_conflict` (422). Unsupported
PATCH fields return `unsupported_lease_patch` (422) and are never ignored.

---

### `routes/inference.py` (`src/pitwall/api/routes/inference.py`)

`POST /v1/inference` — idempotency check via `Idempotency-Key` header or
`body.idempotency_key`; canonical JSON comparison of non-control fields.
Control fields: `{"capability_id","capability","capability_name","provider_id","dry_run","idempotency_key"}`.

Flow: `resolve_inference_target()` → CapabilityNotFound/CapabilityDisabled/
ProviderUnavailable/ProviderNotFound. dry_run → JSON with selected_provider_id.
Success → `run_sync_inference()` + `record_inference_trace()`.
Response headers: `X-Pitwall-Workload-ID`, `X-Pitwall-Capability`, `X-Pitwall-Trace`.

---

### `routes/openai.py` (`src/pitwall/api/routes/openai.py`)

All HTTP methods at `/v1/openai/{capability}/v1/{path:path}` (line 156-159).
Resolves provider chain, inserts passthrough workload, rewrites headers
(`x-pitwall-capability`, `x-pitwall-trace`), calls `execute_openai_with_fallback()`.
On failure: `transition_to_failed()` + `ProviderUnavailable(503)`.
On success: `transition_to_completed()` + `emit_inference_trace()`.
Returns `StreamingResponse` with upstream bytes.
Supports `x-pitwall-drill: skip-primary` to skip first provider.
Fallback budget: `DEFAULT_OPENAI_FALLBACK_BUDGET_S` (60s).

---

### `routes/jobs.py` (`src/pitwall/api/routes/jobs.py`)

`GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/status`, `GET /v1/jobs/{id}/result`
(409 if non-terminal — terminal states: COMPLETED, FAILED, CANCELLED, TIMED_OUT),
`POST /v1/jobs/{id}/cancel` (idempotent on terminal states; calls
`QueueClient.cancel()` for RUNNING, may raise `RateLimited(503)`).

---

### `routes/webhook_subscriptions.py` (`src/pitwall/api/routes/webhook_subscriptions.py`)

`POST /v1/webhook-subscriptions` (201), `GET /v1/webhook-subscriptions`
(query: consumer, active_only). `hmac_secret` stored but never returned.

---

### `admin/audit_capability.py` (`src/pitwall/api/admin/audit_capability.py`)

`POST /v1/admin/audit-capability/{name}` — delegates to `pitwall.audit.capability.audit_capability()`.
Accepts optional query params as payload dict. Returns `CapabilityAuditResult.model_dump()`.

---

### `admin/emergency.py` + `admin/kill_switch.py`

`POST /v1/admin/kill-switch` (`emergency.py:87`). `KillSwitchRequest`:
`reason: str (required)`, `terminate_compute: bool = True`.

`run_kill(reason, actor, *, terminate_compute)` — builds `TailscaleNetworkSever`
from complete `TAILSCALE_OAUTH_CLIENT_ID/SECRET/TAILNET` config or falls back to
`NoOpNetworkSever`, calls `CloudKillSwitch.activate()`, persists `KillReport` to
`pitwall.kill_log` via `persist_kill_report()`.

`CloudKillSwitch.activate(reason)` (`kill_switch.py:191`): three-step (ACL deny,
device revoke, pod terminate) + best-effort R2 staging cleanup. Never raises;
returns `KillReport` with `errors` list on partial failure.

---

### `leases/launch.py` (`src/pitwall/api/leases/launch.py`)

```
ensure_launch_template(pool, capability, provider) -> LaunchTemplate
    Validates POD_LEASE; creates/resolves RunPod template.

prepare_lease_launch(pool, capability, provider, *, request_id, extra_env)
    -> LeaseLaunchPlan  [line 570]

run_launch(pool, capability, provider, *, request_id=None, extra_env=None,
           payload=None, budget_gate=None, idempotency_key=None, dry_run=False)
    -> dict[str, Any]  [line 849]
    admit_lease_launch() → prepare_lease_launch() →
    create_pod_with_fallback() → _persist_ready_lease()
    (CREATING→WAITING_RUNTIME→WAITING_PROBE→ACTIVE).
    Returns {lease_id, pod_id, workload_id, template_id, etc.}.

estimate_lease_launch_cost(capability, provider, payload=None) -> Decimal
```

Error classes: `LaunchConfigError`, `InvalidProviderConfig`, `ProviderNotPodLease`,
`TemplateImageNotConfigured`.

**Invariant:** `_env_for_pod()` blocks override of `PITWALL_*` identity keys and
`AWS_*`/`R2_*` storage credential keys by `extra_env` or `provider.config.env_vars`.

---

### `leases/teardown.py` (`src/pitwall/api/leases/teardown.py`)

```
run_teardown(lease_id, *, pool, redis_client=None, reason=None,
             now=None, terminal_state=LeaseState.STOPPED) -> LeaseTeardownResult  [line 48]
    Fetch lease → no-op if TERMINAL_LEASE_STATES. Mark ACTIVE→STOPPING.
    terminate_pod() → close_lease_cost() → repo.close_teardown() →
    publish to Redis LEASE_TERMINATED_CHANNEL.

LeaseTeardownResult = dataclass(lease, event, published_subscribers)
LEASE_TERMINATED_CHANNEL = "pitwall:lease:terminated"
```

`close_lease_cost()`: rate from `provider.config.cost.per_second_active`, elapsed =
`terminated_at - created_at`.

---

### `schemas/` — Pydantic request/response models

| File | Key types |
|---|---|
| `capability_schemas.py` | `CapabilityCreate`, `CapabilityPatch`, `CapabilityListFilter`, `CapabilityResponse` |
| `provider_schemas.py` | `ProviderCreate`, `ProviderPatch`, `ProviderListFilter`, `ProviderResponse`, `ProviderHealthResponse`, `validate_provider_registration_config()`, `EndpointRegistrationConfig`, `EndpointRegistrationRequest` |
| `leases.py` | `LeaseCreate`, `LeasePatch`, `LeaseResponse`, `LeaseRenew`, `LeaseStop`, `lease_patch_conflicting_fields()` |
| `inference.py` | `InferenceRequest` (extra="allow", AliasChoices on capability_id), `InferenceResponse` |
| `jobs.py` | `JobSubmitRequest`, `JobResponse` |

`LeasePatch` + `lease_patch_conflicting_fields()` detects multi-axis PATCH
(image+GPU+volume axes).

---

## 3. Route Inventory

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/healthz`, `/health` | — | `{"ok": true}` |
| GET | `/v1/health` | — | postgres + redis check |
| POST | `/v1/admin/capabilities` | Secret | 201, duplicate→409 |
| PATCH | `/v1/admin/capabilities/{id}` | Secret | blocks name/version/class_/cost_mode |
| POST | `/v1/admin/capabilities/{id}/enable` | Secret | |
| POST | `/v1/admin/capabilities/{id}/disable` | Secret | |
| GET | `/v1/capabilities` | — | list with CapabilityListFilter |
| GET | `/v1/capabilities/{name}` | — | by name, not id |
| POST | `/v1/admin/providers` | Secret | 201; validates config |
| PATCH | `/v1/admin/providers/{id}` | Secret | validates config on patched fields |
| POST | `/v1/admin/providers/{id}/enable` | Secret | |
| POST | `/v1/admin/providers/{id}/disable` | Secret | |
| POST | `/v1/admin/providers/{id}/hibernate` | Secret | |
| GET | `/v1/providers` | — | list with ProviderListFilter |
| GET | `/v1/providers/{id}` | — | |
| GET | `/v1/providers/{id}/health` | — | |
| POST | `/v1/leases` | — | 201; dry_run returns plan |
| GET | `/v1/leases/{id}` | — | |
| PATCH | `/v1/leases/{id}` | API token | Atomic policy/auto-teardown update; unsupported fields → 422 |
| POST | `/v1/leases/{id}/renew` | API token | Atomic additive renewal; optional idempotency key |
| POST | `/v1/leases/{id}/stop` | — | calls run_teardown() |
| DELETE | `/v1/leases/{id}` | — | 204, idempotent |
| POST | `/v1/inference` | — | idempotency via header or body |
| GET/POST/PUT/DELETE/PATCH/OPTIONS | `/v1/openai/{capability}/v1/{path:path}` | — | passthrough |
| GET | `/v1/jobs/{id}` | — | |
| GET | `/v1/jobs/{id}/status` | — | |
| GET | `/v1/jobs/{id}/result` | — | 409 if non-terminal |
| POST | `/v1/jobs/{id}/cancel` | — | idempotent on terminal states |
| POST | `/v1/webhook-subscriptions` | — | 201 |
| GET | `/v1/webhook-subscriptions` | — | query: consumer, active_only |
| POST | `/v1/admin/audit-capability/{name}` | Secret | |
| POST | `/v1/admin/kill-switch` | Secret | |

Auth columns identify the principal scope. When bearer authorization is enabled,
`ApiTokenMiddleware` requires `Authorization: Bearer <token>` on every non-health
row and returns 403 when the authenticated token lacks that route's scope.

---

## 4. Public Interfaces

```python
# app.py
app: FastAPI                                             # server startup
AdminSecretMiddleware(app, secret)                       # fail-closed admin gate
ApiTokenMiddleware(app, authorizer)                      # scoped bearer gate
InboundRateLimitMiddleware(app, config, authorizer)      # default inbound limiter

# exceptions.py
PitwallApiError, CapabilityNotFound, CapabilityDisabled, CapabilityConflict,
ProviderNotFound, ProviderUnavailable, ProviderConflict, RateLimited,
LeaseNotFound, LeaseStateConflict, ChangeSetTooBroad, IdempotencyMismatch,
WorkloadNotFound, JobNotReady

# leases/
run_launch(pool, capability, provider, **kwargs) -> dict[str, Any]
run_teardown(lease_id, **kwargs) -> LeaseTeardownResult
LeaseTeardownResult = dataclass(lease, event, published_subscribers)

# admin/emergency.py
run_kill(reason, actor, *, terminate_compute=True) -> KillReport

# admin/kill_switch.py
KillReport = dataclass(triggered_at, reason, tailscale_acl_updated,
                       devices_removed, pods_terminated, total_duration_ms, errors)

# schemas/
validate_provider_registration_config(**kwargs)  # raises ValueError
lease_patch_conflicting_fields(patch) -> list[str]
```

---

## 5. Configuration

**Required at import** (`src/pitwall/api/app.py:41`): `RUNPOD_API_KEY`,
`DATABASE_URL`, `REDIS_URL`.

**Optional at import:**

| Variable | Default | Effect |
|---|---|---|
| `PITWALL_ADMIN_SECRET` | unset | Configures the admin secret. `AdminSecretMiddleware` is always installed; unset means `/v1/admin/*` fails closed with 401 (`src/pitwall/api/app.py:47`, `src/pitwall/api/app.py:162`, `src/pitwall/api/app.py:290`). |
| `PITWALL_API_TOKEN` | unset | All-scopes operator bearer token; required for non-loopback API binding. |
| `PITWALL_API_SCOPED_TOKENS` | unset | JSON object mapping opaque tokens to explicit API scopes. |
| `PITWALL_INBOUND_RATE_LIMIT` | `120/60s` | Inbound REST rate limit; explicit `off`, `disabled`, or `none` disables it. |

**Runtime** (`config.py:424-536` via `load_settings_from_env()`):

| Variable | Default | Used by |
|---|---|---|
| `RUNPOD_REST_API_URL` | `https://rest.runpod.io/v1` | RunPod client |
| `RUNPOD_NETWORK_VOLUME_ID` | — | Pod lease launch |
| `LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY` | — | Inference tracing |
| `R2_ENDPOINT/ACCESS_KEY/SECRET_KEY` | — | Kill-switch R2 cleanup |
| `R2_BUCKET_STAGING` | `pitwall-staging` | Kill-switch R2 cleanup |
| `TAILSCALE_OAUTH_CLIENT_ID/SECRET/TAILNET` | — | Kill-switch |
| `PITWALL_MONTHLY_BUDGET_USD` | `50.0` | Budget gate |
| `PITWALL_PER_REQUEST_MAX_USD` | `10.0` | Per-request ceiling |
| `PITWALL_DEFAULT_LEASE_TTL_S` | `7200` | Lease TTL |

---

## 6. Failure Modes & Error Types

All API exceptions inherit `PitwallApiError`. Error envelope: `{"error": "<code>", ...}`.

| Exception | Trigger | HTTP |
|---|---|---|
| `CapabilityNotFound` | `repo.get_by_name()` → None | 404 |
| `CapabilityDisabled` | `CapabilityDisabledError` from resolver | 409 |
| `CapabilityConflict` | Duplicate name on create | 409 |
| `ProviderNotFound` | `repo.get()` → None | 404 |
| `ProviderUnavailable` | `NoHealthyProviderError` from resolver | 503 |
| `ProviderConflict` | Duplicate name on create | 409 |
| `RateLimited` | `QueueClient.cancel()` raises | 503 |
| `LeaseNotFound` | `repo.get()` → None | 404 |
| `LeaseStateConflict` | Teardown non-ACTIVE lease | 409 |
| `ChangeSetTooBroad` | PATCH spans image+GPU+volume | 400 |
| `IdempotencyMismatch` | Reuse key, different body | 422 |
| `WorkloadNotFound` | `repo.get(workload_id)` → None | 404 |
| `JobNotReady` | GET /result on non-terminal state | 409 |
| `BudgetRejected` | Budget gate rejects launch | 402 |

**Edge cases:** `POST /v1/leases` + `dry_run=True` → launch plan, no lease persisted.
`DELETE /v1/leases/{id}` is idempotent (suppresses `LeaseNotFound`).
`POST /v1/jobs/{id}/cancel` on terminal state → returns current workload (no-op).
`run_teardown()` with no `redis_client` → logs warning, returns 0, no exception.

---

## 7. Testing

**`tests/api/`** (contract + integration):

`test_capabilities_contract.py`, `test_providers_contract.py`, `test_leases_contract.py`,
`test_inference_contract.py` (idempotency, dry_run, budget rejection),
`test_jobs_contract.py`, `test_webhook_subscriptions_contract.py`,
`test_admin_auth_matrix.py` (all `/v1/admin/*` — missing/wrong/correct secret),
`test_error_envelope.py`, `test_openapi_snapshot.py`, `test_route_inventory.py`,
`test_route_precedence.py`,
`test_e2e_lease_lifecycle.py`, `test_e2e_sync_inference.py`, `test_e2e_async_job_webhook.py`,
`test_openai_proxy.py`, `test_openai_proxy_fallback.py`, `test_openai_proxy_trace.py`,
`test_inference_langfuse.py`, `test_budget_trace.py`, `test_e5_audit_checks.py`,
`test_jobs_read_cancel.py`.

**`tests/admin/` + `tests/security/`**: `test_kill_switch_route.py`,
`test_admin_auth_surface.py`, `test_webhook_receiver_signed.py`, `test_schemathesis_fuzz.py`.

**Other:** `tests/audit/test_rest_audit_mode.py`, `tests/release/test_dry_run_tier.py`,
`tests/integration/test_reconcile_idempotency_concurrency.py`.

---

## 8. Dependencies

**Internal:** `pitwall.config` (`require_runtime_env`, `load_settings_from_env`),
`pitwall.core.models` (Capability, Provider, Lease, etc.), `pitwall.core.enums`
(LeaseState, ProviderType, etc.), `pitwall.core.ids` (`ulid_new()`),
`pitwall.db.repository` (all repositories + `insert_audit()`),
`pitwall.db.kill_log` (`persist_kill_report()`), `pitwall.cost` (`BudgetRejected`, `BudgetGate`),
`pitwall.cost.sync_gate` (`estimate_cost()`),
`pitwall.core.inference` (`resolve_inference_target()`, `run_sync_inference()`, `record_inference_trace()`),
`pitwall.resolver` (resolver exception types),
`pitwall.routing.fallback` (`execute_openai_with_fallback()`),
`pitwall.routing.openai` (`resolve_openai_provider_chain()`),
`pitwall.runpod_client.pods` (pod creation/termination),
`pitwall.runpod_client.templates` (template management),
`pitwall.runpod_client.queue` (`QueueClient`),
`pitwall.runpod_client.gpu` (GPU name validation),
`pitwall.workload_lifecycle` (workload state transitions),
`pitwall.leases.state` (`transition_lease_state()`, `TERMINAL_LEASE_STATES`),
`pitwall.observability.langfuse` (`emit_inference_trace()`),
`pitwall.audit.capability` (`audit_capability()`),
`pitwall.r2_temp_credentials` (`vend_r2_temp_credential_pod_env()`),
`pitwall.r2_staging_cleanup` (`cleanup_staging_for_pods()`).

**External:** `fastapi`, `pydantic` v2, `starlette` (ASGI types), `asyncpg`, `redis.asyncio`,
`httpx`, `hmac` (`compare_digest`).
