# Provider Plugins — SDLC Design Document

## 1. Purpose & Scope

`pitwall.providers` is the provider-plugin seam for compute backends. The seam separates a persisted `pitwall.core.models.Provider` fulfillment record from the runtime adapter that knows how to provision, inspect, reconcile, price, and tear down provider resources.

The current built-in adapters are:

- `runpod` — delegates to existing RunPod launch/status/reconcile/teardown services.
- `vast` — talks directly to Vast.ai REST endpoints and converts hourly price fields into `PerSecondPricing`.
- `lambda_cloud` — talks directly to Lambda Cloud REST endpoints and uses flat `PerVmSecondPricing`.

Adapters must use header-based authentication and must never place secrets in URLs.

---

## 2. Interface

Every adapter implements `pitwall.providers.interface.Provider`:

```python
class Provider(Protocol):
    id: str
    name: str
    credential_schema: type[pydantic.BaseModel]

    def pricing_model(capability, provider_record) -> TaggedPricingModel: ...
    async def provision(ProvisionRequest) -> ProvisionResult: ...
    async def status(StatusRequest) -> StatusResult: ...
    async def reconcile(ReconcileRequest) -> ReconcileResult: ...
    async def teardown(TeardownRequest) -> TeardownResult: ...
```

`ProviderRegistry` stores adapters by stable id, rejects duplicate ids, validates credentials through the adapter schema, and exposes safe JSON schemas for operator UIs. `create_default_registry()` registers built-ins in order: RunPod, Vast, Lambda Cloud.

---

## 3. Lambda Cloud Adapter

`LambdaCloudProvider` is registered under id `lambda_cloud`. It uses `LambdaCloudCredentials`, which requires `api_key: SecretStr` and accepts an optional `lambda_api_url` override defaulting to `https://cloud.lambda.ai/api/v1`. The URL validator requires an absolute HTTP(S) URL with no userinfo, query string, or fragment. Requests send the API key only as `Authorization: Bearer ...`.

Pricing is `PerVmSecondPricing`. Provider config should use:

```json
{
  "cost": {
    "kind": "per_vm_second",
    "rate_per_second": "0.00016"
  }
}
```

The adapter also accepts untagged `rate_per_second`, `per_vm_second`, `price_per_second`, or `price_usd_per_second` cost fields and converts hourly aliases (`price_per_hour`, `rate_per_hour`, `price_usd_per_hour`) into a VM-second rate. Any other tagged variant is rejected for Lambda Cloud.

Provision builds a launch body from `provider.config.launch` plus request payload overrides from `lambda_launch`, `instance`, `launch`, or whitelisted top-level launch fields. Required launch fields are `region_name`, `instance_type_name`, and non-empty `ssh_key_names`; `provider.region` fills `region_name` when omitted. If `name` is omitted, Pitwall generates `pitwall-{provider_id}-{request_id-or-random}`.

Network operations:

- `POST /instance-operations/launch` launches a VM and returns the first `data.instance_ids` value as `external_id`.
- `GET /instances/{id}` maps Lambda statuses to provider-neutral `ResourceStatus`.
- `GET /instances` reconciles the account-visible VM list when no explicit ids are supplied.
- `POST /instance-operations/terminate` tears down one VM with `{"instance_ids": [external_id]}`.

Until the lease table has a generic external-resource column, Lambda Cloud leases use the same legacy `runpod_pod_id` column as Vast to store the provider VM id.

---

## 4. Status & Reconcile

Lambda Cloud status mapping:

| Lambda status | Pitwall status |
|---|---|
| `booting`, `creating`, `launching`, `pending`, `provisioning`, `starting` | `provisioning` |
| `active`, `ready`, `running` | `running` |
| `terminating`, `terminated`, `deleted` | `terminated` |
| `preempted`, `unhealthy`, `failed`, `error` | `failed` |
| anything else | `unknown` |

Reconcile marks failed provider resources as persisted lease state `failed` when a database pool is available. `preempted` resources get `raw["pitwall_preempted"] = true` and `raw["pitwall_safe_state"] = "failed"` so operators can distinguish provider preemption from ordinary health failure.

---

## 5. Testing

Lambda Cloud tests are hermetic and use `httpx.MockTransport`; they do not require live network or database access. Coverage includes registry registration, credential URL safety, per-VM-second pricing, launch body construction, header auth, dry run, status mapping, missing-resource handling, reconcile failure convergence, teardown, and a property test for flat VM-second pricing invariants.

---

## 6. Drift Detection

`pitwall.providers.drift` implements deterministic, read-only provider drift detection.  It compares a persisted :class:`pitwall.core.models.Provider` record (expected state) against a live :class:`ProviderObservedState` snapshot and emits structured :class:`DriftFinding` objects.

### Key types

```python
class DriftSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

@dataclass(frozen=True, slots=True)
class DriftFinding:
    provider_id: str
    field: str
    expected: Any
    observed: Any
    severity: DriftSeverity
    message: str = ""

@dataclass(frozen=True, slots=True)
class ProviderObservedState:
    provider_id: str
    status: ResourceStatus | None = None
    price_per_second: Decimal | None = None
    availability: bool | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
```

### Entry point

```python
def detect_drift(
    provider: Provider,
    observed: ProviderObservedState,
    *,
    capability: Capability | None = None,
) -> list[DriftFinding]: ...
```

The function evaluates four dimensions in a fixed order:

1. **provider_id** — a critical finding is emitted when the observed snapshot does not belong to the expected provider.
2. **enabled** — a disabled provider with a running resource yields ``HIGH``; an enabled provider with a terminated resource yields ``MEDIUM``.
3. **health_status** — the persisted ``health_status`` string is normalised to ``healthy`` / ``unknown`` / ``unhealthy`` and compared against the observed :class:`ResourceStatus`.  A mismatch yields ``HIGH``.
4. **price_per_second** — when *capability* is supplied and the provider uses a time-based pricing model (``GpuHourPricing``, ``PerSecondPricing``, or ``PerVmSecondPricing``), the configured rate is compared exactly against the observed rate.  A mismatch yields ``MEDIUM``.
5. **availability** — an enabled provider that is live-unavailable yields ``HIGH``; a disabled provider that is live-available yields ``LOW``.

### Snapshot helpers

`observe_from_status_result` builds a :class:`ProviderObservedState` from a single :class:`StatusResult`.

`observe_from_runpod_snapshot` normalises a :class:`GpuDiscoverySnapshot` into observed price and availability for a RunPod provider:

- **Price** is converted from the live hourly rate to per-second by dividing by 3600.
- **Availability** is ``True`` when the configured GPU type is reported as available in the configured datacenter.  When no datacenter is pinned, availability is ``True`` if the GPU is available in *any* datacenter.

### Invariants

- ``detect_drift`` is deterministic: the same ``(provider, observed, capability)`` inputs always produce the same ordered list of findings.
- The function is read-only and performs no I/O.
- No ``# type: ignore`` suppressions are used in the module.

### Testing

The drift subsystem is covered by hermetic unit tests in ``tests/providers/test_drift.py``:

- identity-mismatch guard
- enabled drift (disabled + running, enabled + terminated)
- health-status drift (healthy vs failed, unknown vs running, unhealthy vs running)
- price drift (match, mismatch, missing capability, missing observed price, unsupported pricing model)
- availability drift (enabled + unavailable, disabled + available, missing observed availability)
- RunPod snapshot helper (price conversion, datacenter availability, fallback to any datacenter)
- status-result helper
- determinism property test (Hypothesis)

---

## 7. Provider Wave-2 Feasibility

### Overview

This feasibility spike assesses 2-3 additional providers (Paperspace, Modal, CoreWeave) against the Provider interface. The analysis is codified in `src/pitwall/providers/_wave2_feasibility.py` as typed `CandidateAssessment` records consumed by the SDLC doc generator and the hermetic test suite.

### Typed data structures

Every candidate is represented as a frozen, slotted `CandidateAssessment` dataclass:

```python
@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    candidate_id: str
    name: str
    url: str
    auth_fit: AuthFit            # auth type, bearer compatibility, notes
    pricing_fit: PricingFit       # alignment, compatible_kinds, notes
    lease_model: LeaseModel      # gpu_lease_second | serverless_invocation | kubernetes_pod | …
    interface_fit: InterfaceFit  # per-method gap tuples + summary notes
    effort_rating: EffortRating  # 1_trivial … 5_major
    fit_rating: FitRating         # 5_excellent … 1_incompatible
    summary: str
    blocking_issues: tuple[str, ...]
```

Supporting enums: `AuthType`, `PricingAlignment`, `LeaseModel`, `EffortRating`, `FitRating`.

### Candidate summary

| Candidate | Auth | Pricing alignment | Lease model | Fit rating | Effort rating | Blocking? |
|---|---|---|---|---|---|---|
| Paperspace | Header Bearer | `conversion_required` (per-second/per-vm-second) | `gpu_lease_second` | `3_moderate` | `4_high` | REST API unconfirmed; SDK-first |
| Modal | Header Bearer | `conversion_required` (per-second/per-request/per-token) | `serverless_invocation` | `2_poor` | `5_major` | Serverless model — no persistent resources |
| CoreWeave | Header Bearer | `direct` (per-second) | `kubernetes_pod` | `3_moderate` | `4_high` | Kubernetes-first; REST API unconfirmed |

### Paperspace

**Auth:** Header Bearer — `x-api-key` / `Authorization: Bearer`. No OAuth2 or AWS SIG4.

**Pricing:** On-demand GPU instances billed per-second. Hourly list price ÷ 3600 → `PerSecondPricing`. Spot instances support bid pricing → `PerSecondPricing.bid_rate_per_second`. Direct `PerVmSecondPricing` fit for flat-rate VM tiers.

**Interface gaps:**
- No confirmed public REST API for instance lifecycle; Gradient SDK is the documented control plane.
- Lease lifecycle (create/wait/runtime/active) needs custom mapping.
- No bulk instance listing confirmed; preemption signal not surfaced.

**Blocking:** Without a stable public REST API, a Paperspace adapter cannot follow the REST/httpx pattern of all four Wave-1 providers. If a REST API is confirmed, effort drops to `3_medium`.

### Modal

**Auth:** Header Bearer — `x-modal-api-key` or `Authorization: Bearer`.

**Pricing:** Per-second container wall-time maps to `PerSecondPricing`. Per-invocation endpoints map to `PerRequestPricing`. Token-based endpoints (when exposed) map to `PerTokenPricing`.

**Interface gaps:**
- Modal is a serverless platform — there are no persistent GPU leases.
- No `provision()` equivalent (deploys are declarative, not resource-lease).
- No `status()` to poll — function calls complete or fail with no fleet view.
- No `reconcile()` — stateless serverless model.
- No `teardown()` — calls run to completion; no persistent lease to terminate.

**Blocking:** Modal's serverless programming-model architecture is fundamentally incompatible with the persistent GPU lease lifecycle the Provider protocol assumes. A Modal-specific abstraction outside the Provider plugin seam would be more appropriate.

### CoreWeave

**Auth:** Header Bearer — `Authorization: Bearer` API tokens. Kubernetes kubeconfig is a separate auth plane.

**Pricing:** Hourly GPU rates ÷ 3600 → `PerSecondPricing`. Spot/preemptible → `PerSecondPricing.bid_rate_per_second`. Direct fit; no per-token or per-request in core GPU compute.

**Interface gaps:**
- CoreWeave's idiomatic control plane is Kubernetes (kubectl / official kubernetes-python client), not a REST adapter.
- Instance lifecycle REST API at `cloud.coreweave.com` is unconfirmed.
- Lease TTL/expiry requires explicit Kubernetes runtime management (TTL Policy, finalizers).
- Preemption signal available via `Pod.preemptionPolicy`.

**Blocking:** If CoreWeave recommends kubectl/kubeconfig as the primary control path, a `KubernetesRuntimeProvider` outside the REST adapter seam would be more idiomatic than a direct Provider adapter. A confirmed REST API for instance lifecycle would reduce effort to `3_medium`.

### Effort / Fit matrix

```
                  Effort
              1    2    3    4    5
         +----+----+----+----+----+
    Fit 5 |    |    |    |    |    |
         +----+----+----+----+----+
        4 |    |    |    |    |    |
         +----+----+----+----+----+
    3    |    |Paperspace|    |Core|
         |    |    |    |    |Weave
         +----+----+----+----+----+
        2 |    |    |    |    |Modal|
         +----+----+----+----+----+
        1 |    |    |    |    |    |
         +----+----+----+----+----+
```

### Recommendations

1. **CoreWeave** is the most promising Wave-2 candidate — direct pricing alignment, bearer auth, and a GPU lease model compatible with the Provider lifecycle. Priority action: confirm `cloud.coreweave.com` REST API scope for instance lifecycle.

2. **Paperspace** is the second priority — strong pricing alignment and bearer auth, but the REST API confirmation is a hard prerequisite before any adapter work begins.

3. **Modal** is out of scope for the Provider plugin seam. A separate serverless inference abstraction (outside `pitwall.providers`) would be the appropriate integration point if Modal support is desired.

### Test coverage

Hermetic tests in `tests/providers/test_wave2_feasibility.py` cover:
- All three candidates present in `WAVE2_CANDIDATES`
- `candidate_by_id()` returns correct records
- All enum fields are valid (no `type: ignore` suppressions)
- All tuple fields are non-empty with non-blank string entries
- Auth, pricing, interface, and rating sub-structures are well-formed
- Blocking issues exist for all candidates
- `Paperspace.pricing_fit.compatible_kinds` includes `per_second`
- `Modal.pricing_fit.compatible_kinds` includes `per_second` and `per_request`
- `CoreWeave.pricing_fit.alignment` is `direct`
- Self-consistency: `candidate_by_id(c.candidate_id) is c` for all candidates

Run with: `pytest tests/providers/test_wave2_feasibility.py -q`
Hermetic only (no `integration`, `slow`, or `live` markers).
