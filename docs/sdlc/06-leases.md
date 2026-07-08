# Pod-Lease Lifecycle Subsystem

## 1. Purpose & Scope

Manages RunPod GPU broker lease lifecycle: creation through teardown. A lease is a stateful `Lease` row representing an allocated RunPod pod. Subsystem owns: state machine transitions, pod creation orchestration (`launch.py`), single-lease teardown (`teardown.py`), and reconciler-triggered auto-teardown on TTL expiry. Does **not** own account-wide kill switches or billing computation.

---

## 2. Components

### `src/pitwall/leases/state.py`

Enforces the lease state transition graph. All persisted lease state changes must flow through here.

**Public API:**

```python
def transition_lease_state(from_state: LeaseStateInput, to_state: LeaseStateInput) -> LeaseState
# Validates against LEASE_STATE_TRANSITIONS; raises IllegalLeaseTransitionError on illegal jumps.
# Returns the validated LeaseState — safe to persist.

def can_transition_lease(from_state: LeaseStateInput, to_state: LeaseStateInput) -> bool
# Returns True iff the transition is permitted.
```

**Exception hierarchy:**

```python
class LeaseTransitionError(RuntimeError):
    error_code = "lease_transition_error"

class IllegalLeaseTransitionError(LeaseTransitionError):
    error_code = "illegal_lease_transition"
    def __init__(self, from_state: LeaseState, to_state: LeaseState) -> None: ...
    def to_dict(self) -> dict[str, str]: ...
# Alias: InvalidLeaseTransitionError = IllegalLeaseTransitionError
```

**State sets:**

```python
TERMINAL_LEASE_STATES = frozenset({LeaseState.STOPPED, LeaseState.FAILED, LeaseState.EXPIRED})
ACTIVE_LEASE_STATES = frozenset({
    LeaseState.CREATING, LeaseState.WAITING_RUNTIME,
    LeaseState.WAITING_PROBE, LeaseState.ACTIVE, LeaseState.STOPPING,
})
```

**Transition map** (`LEASE_STATE_TRANSITIONS`):

| From | Allowed `to` |
|---|---|
| `CREATING` | `WAITING_RUNTIME`, `FAILED` |
| `WAITING_RUNTIME` | `WAITING_PROBE`, `FAILED` |
| `WAITING_PROBE` | `ACTIVE`, `FAILED` |
| `ACTIVE` | `STOPPING`, `EXPIRED`, `FAILED` |
| `STOPPING` | `STOPPED`, `EXPIRED`, `FAILED` |
| `STOPPED` / `FAILED` / `EXPIRED` | *(none — terminal)* |

**Invariant:** No transition outside `LEASE_STATE_TRANSITIONS` may be persisted. String values are coerced via `_coerce_lease_state` raising `ValueError` for unknown strings.

---

### `src/pitwall/api/leases/launch.py`

Assembles and executes a RunPod pod launch for a `pod_lease` provider.

**Entry points:**

```python
async def run_launch(
    *, pool, capability, provider,
    request_id: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    payload: Mapping[str, Any] | None = None,
    budget_gate: Any | None = None,
    idempotency_key: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]
# Returns: backend, dry_run, pod_id, lease_id, template_id, workload_id, etc.

async def prepare_lease_launch(pool, capability, provider, *, request_id=None, extra_env=None) -> LeaseLaunchPlan
# Resolves template and assembles env/workload/placement without creating pod.

async def ensure_launch_template(pool, capability, provider) -> LaunchTemplate
# Creates/retrieves RunPod template.

async def admit_lease_launch(pool, capability, provider, *, budget_gate=None, payload=None, idempotency_key=None) -> str
# Admits through budget gate, returns workload_id (format: wkl_<ulid>).

def estimate_lease_launch_cost(capability, provider, payload=None) -> Decimal
```

**Dataclasses:**

```python
@dataclass(frozen=True) class LaunchTemplate:
    template_id, template_name, image_ref, registry_auth_id, container_disk_gb, volume_mount_path

@dataclass(frozen=True) class LeaseLaunchPlan:
    template: LaunchTemplate; env: dict[str, str]; workload: WorkloadConfig;
    network_volume_id, data_center_id, volume_attach_timeout_s
```

**Exceptions:** `LaunchConfigError`, `InvalidProviderConfig`, `ProviderNotPodLease`, `TemplateImageNotConfigured`.

**Internal state sequencing** (`_persist_ready_lease`): on pod readiness, sequences `CREATING -> WAITING_RUNTIME -> WAITING_PROBE -> ACTIVE` via `LeaseRepository.update_state` + `update_readiness`.

**Lease ID format:** `lease_{provider_id_no_dashes}_{uuid_hex_12}`.

**TTL:** `_expiry_for_lease` reads `provider.config['lease_ttl_ms']` or `ttl_ms` (default 7200000ms = 2h).

**Key invariants:**
- Only `pod_lease` provider types accepted; others raise `ProviderNotPodLease`
- Pre-readiness callback runs in a worker thread; persists `Lease` row before the readiness wait (leak-safety); uses `asyncio.run_coroutine_threadsafe` onto the owning loop to avoid `ConnectionDoesNotExistError`
- On `ProviderAttachHangRecoveryRequested`, writes 15-minute cooldown to provider row via `ProviderRepository.patch`

---

### `src/pitwall/api/leases/teardown.py`

Terminates one lease's pod, computes final cost, transitions to terminal, publishes Redis event.

**Entry point:**

```python
async def run_teardown(
    lease_id: str, *,
    pool, redis_client: Any | None = None,
    reason: str | None = None,
    now: dt.datetime | None = None,
    terminal_state: LeaseState | str = LeaseState.STOPPED,
) -> LeaseTeardownResult
# Alias: teardown_lease = run_teardown
```

**Steps:**
1. Fetch lease; raise `LeaseNotFound` if absent
2. If already terminal (`STOPPED`/`EXPIRED`), return early (no-op)
3. Transition `ACTIVE -> STOPPING` via `_mark_stopping`; raises `LeaseStateConflict` if not ACTIVE
4. Call `terminate_pod(stopping.runpod_pod_id)`
5. Compute cost via `close_lease_cost(lease, provider, terminated_at)`
6. Call `lease_repo.close_teardown` transitioning `STOPPING -> terminal_state` with cost/time/reason
7. Publish `lease.terminated` event to Redis channel `pitwall:lease:terminated`

**Result:**

```python
@dataclass(frozen=True) class LeaseTeardownResult:
    lease: Lease; event: dict[str, str | None] | None; published_subscribers: int = 0
```

**Cost computation** (`close_lease_cost`): reads `provider.config['cost']['per_second_active']`; computes `rate * elapsed_seconds`; quantizes to 6 decimal places. Falls back to `lease.cost_accrued_usd` if no rate.

**Event payload** (`lease_terminated_event`):
```python
{"event": "lease.terminated", "lease_id", "provider_id", "runpod_pod_id",
 "state", "terminated_at", "terminated_reason", "cost_accrued_usd"}
```

**Exception:** `TeardownFailed`. Also propagates `LeaseNotFound`, `LeaseStateConflict`.

**Invariant:** `terminal_state` must be `STOPPED` or `EXPIRED`; `_teardown_terminal_state` raises `ValueError` otherwise.

---

### `src/pitwall/api/routes/leases.py`

FastAPI router. Mounts: `POST /v1/leases`, `GET /v1/leases/{id}`, `PATCH /v1/leases/{id}`, `POST /v1/leases/{id}/stop`, `POST /v1/leases/{id}/renew`. Delegates lifecycle operations to `run_launch` and `run_teardown`, and PATCH/renewal to the shared atomic mutation service in `pitwall.leases.mutations`. Uses DI for `LeaseRepository`, `CapabilityRepository`, `ProviderRepository`.

---

## 3. Lease State Machine

**States:**

| State | Meaning |
|---|---|
| `CREATING` | Pod creation request sent; initial row persisted via pre-readiness callback |
| `WAITING_RUNTIME` | Pod is booting; RunPod reports runtime up |
| `WAITING_PROBE` | Pod running; readiness probe not yet satisfied |
| `ACTIVE` | Fully operational; endpoints and readiness confirmed |
| `STOPPING` | Teardown requested; pod termination in progress |
| `STOPPED` | Pod terminated; cost closed — terminal |
| `FAILED` | Creation or runtime failed — terminal |
| `EXPIRED` | TTL elapsed, no renewal — terminal |

**Creation flow:**
1. `run_launch` calls `admit_lease_launch` (budget gate) then `prepare_lease_launch`
2. `create_pod_with_fallback` called with `pre_readiness_callback`; callback persists `Lease` row (state=`CREATING`) in a worker thread via `asyncio.run_coroutine_threadsafe`
3. On pod readiness, `_persist_ready_lease` sequences: `CREATING -> WAITING_RUNTIME -> WAITING_PROBE -> ACTIVE`
4. Readiness signals validated via `LeaseReadiness`; incomplete signals raise `LaunchConfigError`

**Auto-teardown (TTL):** `_expiry_for_lease` sets TTL at creation. Reconciler (`src/pitwall/reconciler/__init__.py` `_fire_lease_expiry_actions`) queries leases where `expires_at <= now` and `auto_teardown_on_expiry = true` and state not terminal; calls `run_teardown(lease_id, terminal_state=LeaseState.EXPIRED, reason="lease_expired")`.

**Mutation contract:** PATCH can persist `renewal_policy` and
`auto_teardown_on_expiry`; launch-shape fields such as image, GPU, template, and
volume are immutable after creation and receive a stable 422. Renewal is
additive from the current persisted expiry, accepts 1–43,200 minutes, and cannot
move expiry beyond 30 days from database time. Both operations use a row lock,
same-transaction audit entry, and optional exactly-once idempotency key. Only
creating, waiting, and active leases are mutable; stopping or terminal leases
return a state conflict.

---

## 4. Public Interfaces

```python
# pitwall.leases.state
next_state = transition_lease_state(from_state: LeaseStateInput, to_state: LeaseStateInput)
ok = can_transition_lease(from_state: LeaseStateInput, to_state: LeaseStateInput)
raise IllegalLeaseTransitionError(from_state, to_state)

# pitwall.api.leases.launch
result = await run_launch(pool=..., capability=..., provider=..., dry_run=False, ...)
plan = await prepare_lease_launch(pool, capability, provider, ...)
template = await ensure_launch_template(pool, capability, provider)
workload_id = await admit_lease_launch(pool, capability, provider, ...)
cost = estimate_lease_launch_cost(capability, provider, payload=None)

# pitwall.api.leases.teardown
result = await run_teardown(lease_id, pool=..., redis_client=None, terminal_state=STOPPED, ...)
result = await teardown_lease(lease_id, pool=..., ...)
cost = close_lease_cost(lease, provider=..., terminated_at=...)
event = lease_terminated_event(lease)
subscribers = await publish_lease_terminated(redis_client, event)
```

---

## 5. Configuration

All config from `provider.config` (JSON `Mapping` on `Provider` model). Defaults applied per-key.

| Config key | Type | Default | Description |
|---|---|---|---|
| `template_name` | `str` | `pitwall-{cap}-{prov}` | RunPod template name |
| `image_ref` | `str` | env `WORKER_IMAGE` | Docker image |
| `container_disk_gb` | `int` | `50` | Container disk GB |
| `volume_mount_path` | `str` | `/workspace` | Volume mount path |
| `network_volume_id` | `str` | env `RUNPOD_NETWORK_VOLUME_ID` | R2 volume |
| `data_center_id` | `str` | env `RUNPOD_DATA_CENTER_ID` | Pod region |
| `gpu_types` / `gpu_type_priority` | `list[str]` | **(required)** | GPU model list |
| `gpu_count` | `int` | `1` | GPU count |
| `gpu_type_priority_mode` | `str` | `custom` | `"custom"` or `"availability"` |
| `data_center_priority` | `str` | `custom` | `"custom"` or `"availability"` |
| `allowed_cuda_versions` | `list[str]` | `None` | CUDA version constraints |
| `ports` | `int\|list[int]\|dict` | `None` | HTTP/TCP port mappings |
| `lease_ttl_ms` / `ttl_ms` | `int` | `7200000` (2h) | Lease TTL ms |
| `max_cost_per_hr` | `float` | `None` | Budget ceiling |
| `max_attach_hang_s` | `float` | `None` | Volume attach timeout |
| `cost.per_second_active` | `Decimal` | `None` | Billing rate |

**Pod env forwarded from process:** `REDIS_URL`, `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `R2_ENDPOINT`, `R2_BUCKET_STAGING`

---

## 6. Failure Modes & Error Types

**Transition:** `IllegalLeaseTransitionError` — `to_dict()` returns `{"error": "illegal_lease_transition", "from_state": "...", "to_state": "..."}`.

**Launch:** `ProviderNotPodLease` (non `pod_lease` provider); `TemplateImageNotConfigured` (no image ref); `InvalidProviderConfig` (malformed config); `LaunchConfigError` (base). `ProviderAttachHangRecoveryRequested` — provider enters 15-min cooldown, response has `provider_fallback=True, provider_cooldown_until`. `ProviderFallbackRequested` — fallback exhausted, `provider_fallback=True`.

**Teardown:** `LeaseNotFound` (404); `LeaseStateConflict` (409 if not ACTIVE on stop); `TeardownFailed` (base).

**API routes:** `ChangeSetTooBroad` (PATCH spanning multiple axes); `LeaseNotFound` (404); `LeaseStateConflict` (409).

**Edge cases:**
- If `pool` lacks `acquire`, `_persist_ready_lease` and `_set_provider_attach_hang_cooldown` silently no-op
- If `redis_client` is `None`, `publish_lease_terminated` logs warning and returns 0 (teardown still succeeds)
- If no billing rate, `close_lease_cost` returns pre-existing `lease.cost_accrued_usd`

---

## 7. Testing

**`tests/unit/leases/test_state_transition_matrix.py`**

Parametrized `test_full_lease_state_transition_matrix`: all `LeaseState` pairs tested. Allowed pairs assert `can_transition_lease` is `True` and `transition_lease_state` returns target. Disallowed pairs assert `can_transition_lease` is `False` and `transition_lease_state` raises `IllegalLeaseTransitionError` with correct `from_state`/`to_state`. `test_expected_matrix_covers_every_lease_state` validates full enum coverage.

**`tests/api/test_leases_contract.py`**

Hermetic route tests for `/v1/leases` POST/GET/PATCH using override dependencies. Tests 404 on missing capability, response shape regression (ensures create returns `LeaseResponse` not raw `run_launch` dict), and contract compliance.

**`tests/release/test_dry_run_tier.py`**

`test_dry_run_leases_returns_template_without_creating_pod`: `dry_run=True` returns template info without calling RunPod. `test_dry_run_lease_no_paid_call_to_runpod_api`: verifies no paid RunPod calls in dry-run mode.

**`tests/test_audit_sixteen_check.py`**

`_pod_lease_provider_fixture()` provides a `pod_lease` provider fixture. Key tests: `test_fail_pod_lease_fixture_without_probe_signal` (readiness validation), `test_fail_pod_lease_fixture_cost_after_readiness` (cost after readiness), `test_fail_pod_lease_attach_timeout_over_five_minutes` (attach hang), `test_fail_pod_lease_long_lived_r2_strategy`, `test_fail_pod_lease_static_r2_env_injection`, `test_fail_missing_single_lease_stop_route`.

---

## 8. Dependencies

**Internal imports:**

| Module | What is used |
|---|---|
| `pitwall.core.enums` | `LeaseState`, `LeaseRenewalPolicy`, `ProviderType` |
| `pitwall.core.models` | `Lease`, `LeaseEndpoints`, `LeaseReadiness`, `Capability`, `Provider` |
| `pitwall.db.repository` | `LeaseRepository`, `ProviderRepository` |
| `pitwall.api.exceptions` | `LeaseNotFound`, `LeaseStateConflict` |
| `pitwall.runpod_client.pods` | `create_pod_with_fallback`, `terminate_pod`, `ProviderAttachHangRecoveryRequested`, `ProviderFallbackRequested` |
| `pitwall.runpod_client.templates` | `ensure_template`, `get_image_ref_from_env`, `get_registry_auth_id_from_env` |
| `pitwall.runpod_client.workloads` | `WorkloadConfig` |
| `pitwall.cost.budget_gate` | `BudgetGate` |
| `pitwall.cost.sync_gate` | `estimate_cost` |
| `pitwall.r2_temp_credentials` | `vend_r2_temp_credential_pod_env` |
| `pitwall.reconciler` | `_fire_lease_expiry_actions` |

**External libraries:** `asyncpg` (Pool), `redis`/`redis.asyncio` (pub/sub), `arq` (`enqueue_submit_runpod_job` in `workload_lifecycle.py`), `pydantic` (all `PitwallModel` subclasses), `fastapi` (router).
