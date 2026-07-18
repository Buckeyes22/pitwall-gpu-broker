# Pre-spend audit & 19-check readiness subsystem

## 1. Purpose & scope

The audit subsystem sits between the REST/MCP control plane and the RunPod execution layer. It provides two complementary views of Pitwall's operational health before a workload is admitted:

- **19-check RunPod audit harness** (`pitwall.audit.sixteen_check`): a hermetic, CI-runnable suite of 19 checks that validate invariants about how Pitwall translates capability configurations into RunPod API calls, how pre-spend payloads are screened for PII/secrets, and whether packaged Policy-as-Code rules allow the configured providers/workloads. It exercises zero live RunPod endpoints — it reads static configuration and source code.
- **Capability pre-spend audit** (`pitwall.audit.capability`): a service that runs the full pre-spend readiness checklist for a named capability, including provider health, pre-spend payload guardrails, cost estimation, monthly budget headroom, and the 19-check result. It backs the REST endpoint `POST /v1/admin/audit-capability/{name}`.

The subsystem does not invoke RunPod execution APIs. It only reads state, runs local estimators, and inspects source tokens.

---

## 2. Components

### `pitwall.audit.sixteen_check` — 19-check RunPod audit harness

**File:** `src/pitwall/audit/sixteen_check.py` (1859 lines)

**Responsibility:** Implements the 19 individual check functions, the `CHECK_FUNCTIONS` registry, `run_all_checks`, `format_report`, the CLI entry point, and the deterministic `scan_pre_spend_payload` scanner used by checks 17/18 and runtime pre-spend guards. Check 19 evaluates packaged Policy-as-Code documents against capability/provider/workload configuration. Each check accepts an `AuditConfig` and raises `CheckFailed` on violation.

**Key types and signatures:**

```python
class AuditSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class PreSpendDecision(StrEnum):
    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"

class PreSpendFindingKind(StrEnum):
    SECRET = "secret"
    PII = "pii"

@dataclass(frozen=True, slots=True)
class PreSpendFinding:
    kind: PreSpendFindingKind
    rule: str
    path: str
    action: PreSpendDecision
    redacted_preview: str
    fingerprint_sha256: str
    def to_dict(self) -> dict[str, str]: ...

@dataclass(frozen=True, slots=True)
class PreSpendPayloadScanResult:
    decision: PreSpendDecision
    findings: tuple[PreSpendFinding, ...]
    redacted_payload: Any
    @property
    def blocked(self) -> bool: ...
    def to_dict(self) -> dict[str, Any]: ...

class CheckFailed(Exception):
    def __init__(
        self,
        check_id: int,
        message: str,
        severity: AuditSeverity = AuditSeverity.HIGH,
        evidence: str | None = None,
        remediation: str | None = None,
    ) -> None: ...

@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: int
    name: str
    passed: bool
    severity: AuditSeverity
    evidence: str
    remediation: str
    message: str = ""

class AuditConfig(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...
    def gpu_ids(self) -> list[str]: ...
    def workloads(self) -> list[dict[str, Any]]: ...
    def launch_params(self) -> dict[str, Any]: ...
    def readiness_config(self) -> dict[str, Any]: ...
    def cost_config(self) -> dict[str, Any]: ...
    def timeout_config(self) -> dict[str, Any]: ...
    def webhook_config(self) -> dict[str, Any]: ...
    def retention_config(self) -> dict[str, Any]: ...
    def volume_config(self) -> dict[str, Any]: ...
    def probe_config(self) -> dict[str, Any]: ...
    def image_config(self) -> dict[str, Any]: ...
    def disk_config(self) -> dict[str, Any]: ...
    def template_config(self) -> dict[str, Any]: ...
    def registry_config(self) -> dict[str, Any]: ...
    def pre_spend_payloads(self) -> list[dict[str, Any]]: ...
    def provider_fixtures(self) -> list[Any]: ...
    def terminate_config(self) -> dict[str, Any]: ...
    def kill_switch_config(self) -> dict[str, Any]: ...

EXPECTED_AUDIT_CHECK_COUNT = 19
CHECK_FUNCTIONS: list[Callable[[AuditConfig], str]]  # 19 members, attrs .check_id set

def scan_pre_spend_payload(payload: Any, *, max_findings: int = 32) -> PreSpendPayloadScanResult: ...
def run_all_checks(cfg: AuditConfig) -> list[CheckResult]: ...
def format_report(results: list[CheckResult]) -> str: ...
def main(argv: list[str] | None = None) -> int: ...
```

**Key invariants enforced:**
- `CHECK_FUNCTIONS` has exactly `EXPECTED_AUDIT_CHECK_COUNT == 19` members (asserted at module load).
- Every function in `CHECK_FUNCTIONS` has a `check_id` integer attribute matching its 1-based index.
- `run_all_checks` never short-circuits; all 19 results are always returned.
- `scan_pre_spend_payload` returns only redacted previews and SHA-256 fingerprints; raw matched PII/secret values are not present in `PreSpendFinding.to_dict()` or `PreSpendPayloadScanResult.to_dict()`.

**CLI entry point:** `python -m pitwall.audit.sixteen_check [--strict] [--json]`. Exits 0 in non-strict mode regardless of pass/fail; in `--strict` mode exits 1 if any check fails. `--json` emits structured JSON.

---

### `pitwall.audit.capability` — Capability pre-spend audit service

**File:** `src/pitwall/audit/capability.py` (682 lines)

**Responsibility:** Implements the eight-check pre-spend readiness evaluation for a named capability. It coordinates capability lookup, provider enumeration, pre-spend payload scanning, cost estimation on the sanitized payload, budget headroom evaluation, and the 19-check result. Designed to be framework-free and used by the REST audit route.

**Key types and signatures:**

```python
CHECK_CAPABILITY_EXISTS = "capability_exists"
CHECK_PROVIDER_CHAIN_NONEMPTY = "provider_chain_nonempty"
CHECK_ALL_PROVIDERS_HEALTHY = "all_providers_healthy"
CHECK_PRE_SPEND_PAYLOAD_GUARDRAIL = "pre_spend_payload_guardrail"
CHECK_COST_ESTIMATE_UNDER_CAP = "cost_estimate_under_cap"
CHECK_MONTHLY_BUDGET_HEADROOM = "monthly_budget_headroom"
CHECK_SIXTEEN_CHECK_AUDIT_PASSED = "16_check_runpod_audit_passed"
CHECK_READY_TO_INVOKE = "ready_to_invoke"
REQUIRED_CHECK_NAMES: tuple[str, ...]  # 8 names above

HEALTHY_PROVIDER_STATUSES: frozenset[str]  # {"healthy"}
ADMITTED_WORKLOAD_STATES: tuple[str, ...]  # ("queued", "running", "completed")

class CapabilityReader(Protocol):
    async def get_by_name(self, name: str) -> Capability | None: ...

class ProviderReader(Protocol):
    async def list(
        self,
        *,
        capability_id: str | None = None,
        enabled_only: bool = False,
        provider_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Provider]: ...

@dataclass(frozen=True, slots=True)
class ProviderEstimate:
    provider_id: str
    estimate_usd: Decimal
    def to_dict(self) -> dict[str, str]: ...

@dataclass(frozen=True, slots=True)
class CapabilityAuditCheck:
    name: str
    passed: bool
    message: str = ""
    warnings: tuple[str, ...] = ()
    estimated_usd: Decimal | None = None
    remaining_usd: Decimal | None = None
    checked_at: dt.datetime | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    @property
    def pass_(self) -> bool: ...  # JSON-safe alias
    def to_dict(self) -> dict[str, Any]: ...
    def model_dump(self, **_: Any) -> dict[str, Any]: ...

@dataclass(frozen=True, slots=True)
class CapabilityAuditResult:
    capability_name: str
    checks: tuple[CapabilityAuditCheck, ...]
    ready_to_invoke: bool
    def to_dict(self) -> dict[str, Any]: ...
    def model_dump(self, **_: Any) -> dict[str, Any]: ...

class CapabilityAuditService:
    def __init__(
        self,
        *,
        capability_repo: CapabilityReader,
        provider_repo: ProviderReader,
        pool: Any | None = None,
        budget_state: BudgetState | None = None,
        environ: Mapping[str, str] | None = None,
        sixteen_check_runner: SixteenCheckRunner = run_all_checks,
        audit_config_factory: AuditConfigFactory = RuntimeAuditConfig,
        now_factory: NowFactory | None = None,
    ) -> None: ...

    async def audit(
        self,
        capability_name: str,
        *,
        payload: EstimatePayload | None = None,
    ) -> CapabilityAuditResult: ...

async def audit_capability(
    capability_name: str,
    *,
    capability_repo: CapabilityReader,
    provider_repo: ProviderReader,
    pool: Any | None = None,
    payload: EstimatePayload | None = None,
    budget_state: BudgetState | None = None,
    environ: Mapping[str, str] | None = None,
    sixteen_check_runner: SixteenCheckRunner = run_all_checks,
    audit_config_factory: AuditConfigFactory = RuntimeAuditConfig,
    now_factory: NowFactory | None = None,
) -> CapabilityAuditResult: ...

async def read_month_to_date_spend_usd(pool: Any) -> Decimal: ...
```

**Key invariants enforced:**
- Exactly 8 checks are always returned (enforced by `REQUIRED_CHECK_NAMES`).
- `pre_spend_payload_guardrail` runs before cost estimation. A `block` decision skips cost estimation and marks the request not ready; a `redact` decision passes and cost estimation uses `redacted_payload`.
- `ready_to_invoke` is True only when all 8 checks pass.
- The service never short-circuits on first failure; all checks are always evaluated.

---

### `pitwall.policy` — Policy-as-Code schema and evaluator

**Files:** `src/pitwall/policy/schema.py`, `src/pitwall/policy/engine.py`, `src/pitwall/policy/loader.py`, `src/pitwall/policy/examples/*.yaml`

**Responsibility:** Defines declarative Policy-as-Code documents and deterministically evaluates them against capability, provider, and workload configuration targets. The evaluator returns a Pydantic `PolicyEvaluationResult` with `allowed`, `decision` (`"allow"` or `"deny"`), and structured `PolicyViolation` findings. Findings include the policy id, target kind/id, path, operator, expected value, sanitized actual value, and message.

**Policy document shape:**

```yaml
version: 1
policies:
  - id: provider.no-static-r2-env
    target: provider
    description: Pod lease providers must not inject long-lived R2 credentials.
    when:
      - path: provider_type
        operator: equals
        value: pod_lease
    rules:
      - path: config.env_vars
        operator: contains_none
        value: ["R2_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"]
        message: pod lease provider injects static R2 credentials
```

**Supported targets:** `capability`, `provider`, `workload`.

**Supported operators:** `exists`, `not_exists`, `equals`, `not_equals`, `in`, `not_in`, `contains`, `not_contains`, `contains_all`, `contains_none`, `gte`, `lte`, `matches`.

**Packaged example policies:** no static R2 credential env keys for pod-lease providers, required pod-lease readiness signals, and minimum vLLM container disk. `check_19_policy_as_code_audit_gate` loads these examples through `load_default_policy_set()` and evaluates them against the same `AuditConfig` object used by the other RunPod audit checks. A deny result raises `CheckFailed(19)` with JSON evidence from `PolicyEvaluationResult.model_dump(mode="json")`; sensitive mapped values are redacted before evidence is emitted.

---

### `pitwall.audit._runtime_config` — Runtime configuration adapter

**File:** `src/pitwall/audit/_runtime_config.py` (212 lines)

**Responsibility:** Implements `AuditConfig` backed by environment variables and hardcoded defaults. Allows the 19-check CLI to run in CI without a live database.

**Signature:**

```python
class RuntimeAuditConfig:
    def get(self, key: str, default: Any = None) -> Any: ...
    def gpu_ids(self) -> list[str]: ...
    def workloads(self) -> list[dict[str, Any]]: ...
    def launch_params(self) -> dict[str, Any]: ...
    def readiness_config(self) -> dict[str, Any]: ...
    def cost_config(self) -> dict[str, Any]: ...
    def timeout_config(self) -> dict[str, Any]: ...
    def webhook_config(self) -> dict[str, Any]: ...
    def retention_config(self) -> dict[str, Any]: ...
    def volume_config(self) -> dict[str, Any]: ...
    def probe_config(self) -> dict[str, Any]: ...
    def image_config(self) -> dict[str, Any]: ...
    def disk_config(self) -> dict[str, Any]: ...
    def template_config(self) -> dict[str, Any]: ...
    def registry_config(self) -> dict[str, Any]: ...
    def pre_spend_payloads(self) -> list[dict[str, Any]]: ...
    def provider_fixtures(self) -> list[dict[str, Any]]: ...
    def terminate_config(self) -> dict[str, Any]: ...
    def kill_switch_config(self) -> dict[str, Any]: ...
```

---

### `pitwall.api.admin.audit_capability` — REST route

**File:** `src/pitwall/api/admin/audit_capability.py` (72 lines)

**Responsibility:** Exposes the capability audit as a FastAPI route. Injects `CapabilityRepository`, `ProviderRepository`, and the asyncpg pool from `app.state`. Delegates entirely to `pitwall.audit.capability.audit_capability`.

**Route:** `POST /v1/admin/audit-capability/{name}` (line 50)

Query parameters are passed through as `payload` to the audit service. Response is `result.model_dump()`.

---

### `pitwall.api.routes.inference` — REST inference pre-spend guard

**File:** `src/pitwall/api/routes/inference.py`

**Responsibility:** `POST /v1/inference` strips control fields (`capability_id`, `provider_id`, `dry_run`, `idempotency_key`) from the raw body and calls `scan_pre_spend_payload()` on the capability params before idempotency replay, capability/provider resolution, cost gating, or RunPod dispatch.

**Decision handling:**

| Scanner decision | Route behavior |
|------------------|----------------|
| `allow` | Continue with original capability params. |
| `redact` | Continue with `PreSpendPayloadScanResult.redacted_payload`; downstream resolver/gate/RunPod call arguments do not receive the original PII substring. |
| `block` | Raise `PreSpendPayloadRejected`, mapped by the standard `PitwallApiError` handler to HTTP 422 with `error="pre_spend_payload_rejected"`, `decision="block"`, and redacted `findings`. Resolver, budget gate, and RunPod calls are not reached. |

---

## 3. The 19 checks

### CHECK_FUNCTIONS registry

```python
CHECK_FUNCTIONS: list[Callable[[AuditConfig], str]] = [
    check_01_gpu_ids_canonical,   # 1
    check_02_cloud_type_volume,   # 2
    check_03_readiness_runtime,   # 3
    check_04_cost_cap_before_readiness,  # 4
    check_05_execution_timeout,   # 5
    check_06_ttl_ge_timeout_plus_queue,  # 6
    check_07_webhook_idempotent_fast200,  # 7
    check_08_retention_windows,   # 8
    check_09_dc_pin,              # 9
    check_10_ssh_first_probe,     # 10
    check_11_image_pull_timeout,  # 11
    check_12_disk_sized,          # 12
    check_13_template_cache,      # 13
    check_14_registry_auth,       # 14
    check_15_terminate_idempotent,  # 15
    check_16_kill_switch_atomic,  # 16
    check_17_pre_spend_secret_guardrail,  # 17
    check_18_pre_spend_pii_redaction,  # 18
    check_19_policy_as_code_audit_gate,  # 19
]
```

Each function carries a `check_id` attribute. The `CHECK_DESCRIPTIONS` dict maps IDs 1–19 to human descriptions.

### Enumerated checks

| ID | Name | Intent |
|----|------|--------|
| 1 | `check_01_gpu_ids_canonical` | All GPU IDs in config and workloads must be in `CANONICAL_GPU_NAMES` (from `pitwall.runpod_client.gpu`). Non-canonical names cause immediate failure. |
| 2 | `check_02_cloud_type_volume` | `cloud_type=ALL` combined with `networkVolumeId` is rejected; RunPod volumes are Secure-Cloud-only so an ALL launch would always fail. The check also verifies `pods._cloud_types_for_rest("ALL", network_volume_id="vol-audit")` returns `["SECURE"]`. |
| 3 | `check_03_readiness_runtime` | Verifies `probe_field == "runtime"` in readiness config; asserts `pods._pod_has_runtime` returns False for `desiredStatus=RUNNING, runtime=None`; asserts True for `runtime` present; validates pod-lease provider fixtures declare all three required signals (`runtime`, `port_mappings`, `probe_2xx`). |
| 4 | `check_04_cost_cap_before_readiness` | `check_order` in cost config must place `cost` before any readiness step. Also verifies `create_pod_with_fallback_sync` source contains `_gate_pod_cost_before_readiness` before `wait_for_pod_runtime_sync`. Pod-lease provider fixtures also checked. |
| 5 | `check_05_execution_timeout` | Both `executionTimeout` and `executionTimeoutMax` must be > 0 and `executionTimeout <= executionTimeoutMax`. |
| 6 | `check_06_ttl_ge_timeout_plus_queue` | `ttl >= executionTimeout + expected_queue_time`. |
| 7 | `check_07_webhook_idempotent_fast200` | Webhook config must have `idempotent=True` and `fast_200=True`. The FastAPI app must have a POST route matching `/webhooks/runpod` or `/runpod`. |
| 8 | `check_08_retention_windows` | `sync_retention_s >= 60`, `async_retention_s >= 1800`, `persist_before_expiry=True`, `sync_persist_deadline_s < 60`, `async_persist_deadline_s < 1800`. |
| 9 | `check_09_dc_pin` | Volume attached with more than one `dataCenterId` is rejected. Pod-lease provider fixture timeouts must be ≤ 300s. Source inspection of `wait_for_pod_runtime_sync` verifies `PodVolumeAttachTimeout` token is present; source of `_run_launch_runpod` and `_set_provider_attach_hang_cooldown` verifies `ProviderAttachHangRecoveryRequested` token. |
| 10 | `check_10_ssh_first_probe` | `ssh_first=True` required; `ssh_localhost` must be in `probe_methods`; `primary_probe` must be `ssh_localhost` or unset; `pods.POD_READINESS_PROBE_ORDER[0] == pods.SSH_LOCALHOST_PROBE_METHOD`. |
| 11 | `check_11_image_pull_timeout` | `image_pull_timeout_s > 0` and `>= startup_timeout_s` (`src/pitwall/audit/sixteen_check.py:1089`, `src/pitwall/audit/sixteen_check.py:1091`, `src/pitwall/audit/sixteen_check.py:1095`). Also verifies the pod env path uses the `StagingStore` seam (`_env_for_pod` calls `get_staging_store().vend_pod_credentials()` and does not call `vend_r2_temp_credential_pod_env` directly), the unconfigured default is `NoOpStagingStore` with empty credential/cleanup behavior, and `CloudflareR2StagingStore` wraps temporary credential vending plus staging cleanup (`src/pitwall/api/leases/launch.py:520`, `src/pitwall/audit/sixteen_check.py:756`, `src/pitwall/audit/sixteen_check.py:758`, `src/pitwall/audit/sixteen_check.py:761`, `src/pitwall/audit/sixteen_check.py:764`, `src/pitwall/audit/sixteen_check.py:766`, `src/pitwall/audit/sixteen_check.py:775`, `src/pitwall/audit/sixteen_check.py:777`, `src/pitwall/staging_store.py:51`, `src/pitwall/staging_store.py:63`). The R2 implementation stays lazy: cleanup imports `cleanup_staging_for_pods`, which imports `boto3` only when the `storage` extra is installed (`src/pitwall/staging_store.py:58`, `src/pitwall/staging_store.py:63`, `src/pitwall/r2_staging_cleanup.py:46`, `pyproject.toml:55`, `pyproject.toml:56`). See `docs/sdlc/15-operations.md:83` and `docs/sdlc/15-operations.md:212` for the R2 temp-credential and staging-cleanup writeup. Current check 11 still verifies the `temp-access-credentials` API fragment, rejects the deprecated R2 token-rotation route, checks session-token env output, rejects provider-injected managed R2 credential keys, and requires temporary R2 strategies (`src/pitwall/audit/sixteen_check.py:744`, `src/pitwall/audit/sixteen_check.py:746`, `src/pitwall/audit/sixteen_check.py:788`, `src/pitwall/audit/sixteen_check.py:800`, `src/pitwall/audit/sixteen_check.py:808`). |
| 12 | `check_12_disk_sized` | All workloads in `per_workload` must meet minimum sizes (vllm≥80GB, embed≥40GB, slim≥20GB). Operator-supplied vLLM provider fixtures must have `container_disk_gb >= 80` and must not use the deprecated Hugging Face CLI. Pitwall does not ship a GPU worker image in the public alpha. |
| 13 | `check_13_template_cache` | `cache_enabled=True`, `create_on_cache_miss=True`, `reuse_on_cache_hit=True`. Source of `templates.ensure_template` must call `_lookup_cached` before `create_template` and must call `_insert_cache`. |
| 14 | `check_14_registry_auth` | `prefix_to_auth_id` mapping must cover `ghcr.io`, `registry.gitlab.com`, `docker.io`. `registry_auth_id_from_env` must select the correct auth ID per prefix. vLLM serverless provider fixtures must have `workers_min == 0` (hibernated fixtures only). |
| 15 | `check_15_terminate_idempotent` | `treat_404_as_success=True` required. A fake 404 is injected into `pods._rest_request` and `pods.terminate_pod_sync` must not raise. Route `/v1/leases/{lease_id}/stop` must exist as POST; `/v1/admin/kill-switch` must exist as POST; single-lease stop must call `run_teardown`; admin kill switch must call `persist_kill_report` and use `rest:admin` audit actor. |
| 16 | `check_16_kill_switch_atomic` | `atomic=True`, steps must be exactly `["list_pods", "terminate_all", "verify"]`, `budget_s < 30`. Lease PATCH route must exist. `lease_patch_conflicting_fields` must reject multi-axis changes (image_ref + gpuTypeIds + volume_id) and accept single-axis changes. PATCH validation must run before the atomic mutation service. |
| 17 | `check_17_pre_spend_secret_guardrail` | `scan_pre_spend_payload` must return `decision="block"` for API-token-shaped strings such as `sk-*` and for PEM private-key material. Every configured `AuditConfig.pre_spend_payloads()` fixture is scanned; any `secret` finding fails the check with critical severity and redacted finding evidence only. |
| 18 | `check_18_pre_spend_pii_redaction` | `scan_pre_spend_payload` must return `decision="redact"` for email PII and must not include the raw email in structured output. Every configured `AuditConfig.pre_spend_payloads()` fixture is scanned; any unredacted PII finding fails the check with high severity and redacted finding evidence only. |
| 19 | `check_19_policy_as_code_audit_gate` | Loads packaged policies from `src/pitwall/policy/examples/*.yaml`, evaluates them against the current `AuditConfig`, and fails with high severity on any deny finding. Evidence is structured JSON and redacts sensitive mapped values. |

### `--strict` CLI contract

```
python -m pitwall.audit.sixteen_check [--strict] [--json]
```

| Flag | Effect |
|------|--------|
| (none) | Reports pass/fail, exits 0 always |
| `--strict` | Exits 0 only if all 19 pass; exits 1 otherwise |
| `--json` | JSON payload (see below); exit code follows `--strict` |
| `--help` | Prints usage, exits 0 |

JSON payload shape (line 1364):

```json
{
  "all_passed": bool,
  "strict": bool,
  "checks": [
    {
      "check_id": int,
      "name": str,
      "passed": bool,
      "severity": str,
      "evidence": str,
      "remediation": str,
      "message": str
    }
  ]
}
```

### REST mode-3 endpoint

`POST /v1/admin/audit-capability/{name}`

- Path param `name`: capability name (e.g. `embedding.bge-m3`)
- Query params: passed through as `payload` to `audit_capability`
- Returns: `CapabilityAuditResult.model_dump()` — dict with `capability_name`, `checks` (list of 8), `ready_to_invoke`
- Each check dict contains: `name`, `pass` (bool), `warnings`, `message`, `estimated_usd`, `remaining_usd`, `checked_at`, `details`
- Status 200 even on `ready_to_invoke=False`; callers inspect the payload

---

## 4. Public interfaces

### From `sixteen_check`

| Symbol | Signature | Caller |
|--------|-----------|--------|
| `EXPECTED_AUDIT_CHECK_COUNT` | `19` | module assertion, capability audit expected-count check |
| `CHECK_FUNCTIONS` | `list[Callable[[AuditConfig], str]]` | `run_all_checks`, tests |
| `CHECK_DESCRIPTIONS` | `dict[int, str]` | `run_all_checks` |
| `CheckFailed` | `Exception` | All 19 check functions |
| `CheckResult` | `@dataclass frozen slots` | `run_all_checks`, `format_report`, `capability` |
| `AuditConfig` | `Protocol` | All check functions |
| `AuditSeverity` | `StrEnum` | `CheckFailed`, `CheckResult` |
| `PreSpendDecision` | `StrEnum("allow", "redact", "block")` | scanner, capability audit, REST inference route |
| `PreSpendFindingKind` | `StrEnum("secret", "pii")` | scanner findings, checks 17/18 |
| `PreSpendFinding` | `@dataclass frozen slots` | scanner result; `to_dict()` is redacted |
| `PreSpendPayloadScanResult` | `@dataclass frozen slots` | scanner result; exposes `blocked` and `to_dict()` |
| `scan_pre_spend_payload(payload)` | `(Any, *, max_findings=32) -> PreSpendPayloadScanResult` | checks 17/18, capability audit, REST inference route |
| `run_all_checks(cfg)` | `(AuditConfig) -> list[CheckResult]` | `CapabilityAuditService._check_sixteen_check_runpod_audit_passed`, `main` |
| `format_report(results)` | `(list[CheckResult]) -> str` | `main` |
| `main(argv)` | `(list[str]\|None) -> int` | `__main__` block |

### From `capability`

| Symbol | Signature | Caller |
|--------|-----------|--------|
| `CapabilityAuditService` | `class` | `audit_capability` helper, tests |
| `CapabilityAuditCheck` | `@dataclass frozen slots` | REST route response |
| `CapabilityAuditResult` | `@dataclass frozen slots` | REST route response |
| `BudgetState` | `@dataclass frozen slots` | `CapabilityAuditService`, `audit_capability` |
| `ProviderEstimate` | `@dataclass frozen slots` | internal |
| `audit_capability(...)` | `async (capability_name, *, ...) -> CapabilityAuditResult` | REST route |
| `read_month_to_date_spend_usd(pool)` | `async (Any) -> Decimal` | `CapabilityAuditService._resolve_budget_state` |
| `REQUIRED_CHECK_NAMES` | `tuple[str, 8]` | tests |
| Check name constants | `CHECK_*` module-level `str` | tests |

### From `policy`

| Symbol | Signature | Caller |
|--------|-----------|--------|
| `PolicySet` | `BaseModel` | policy loader, tests |
| `Policy`, `PolicyCondition`, `PolicyRule` | `BaseModel` | policy loader, tests |
| `PolicyEvaluationResult`, `PolicyViolation` | `BaseModel` | evaluator, check 19 |
| `load_default_policy_set()` | `() -> PolicySet` | check 19 |
| `load_policy_file(path)` | `(str\|Path) -> PolicySet` | tests, operators |
| `evaluate_policies(policy_set, config)` | `(PolicySet, object) -> PolicyEvaluationResult` | check 19 |
| `evaluate_default_policies(config)` | `(object) -> PolicyEvaluationResult` | check 19, tests |

---

## 5. Configuration

### Environment variables read by `_runtime_config.RuntimeAuditConfig`

| Variable | Default | Purpose |
|----------|---------|---------|
| `PITWALL_AUDIT_GPU_IDS` | `NVIDIA H100 80GB HBM3,NVIDIA L4,NVIDIA A100 80GB` | GPU ID list for check 1 |
| `PITWALL_AUDIT_CLOUD_TYPE` | `SECURE` | `cloud_type` for check 2 |
| `RUNPOD_NETWORK_VOLUME_ID` | `""` | Volume ID for checks 2, 9 |
| `RUNPOD_DATA_CENTER_ID` | `""` | DC pin for check 9 |
| `PITWALL_AUDIT_EXEC_TIMEOUT_S` | `3600` | `executionTimeout` for check 5 |
| `PITWALL_AUDIT_EXEC_TIMEOUT_MAX_S` | `7200` | `executionTimeoutMax` for check 5 |
| `PITWALL_DEFAULT_LEASE_TTL_S` | `7200` | `ttl` for check 6 |
| `PITWALL_AUDIT_QUEUE_TIME_S` | `300` | `expected_queue_time` for check 6 |
| `PITWALL_IMAGE_PULL_TIMEOUT_S` | `600` | `image_pull_timeout_s` for check 11 |
| `PITWALL_AUDIT_STARTUP_TIMEOUT_S` | `600` | `startup_timeout_s` for check 11 |
| `RUNPOD_REGISTRY_AUTH_ID_GHCR` | fallback to `RUNPOD_REGISTRY_AUTH_ID` or `"placeholder-ghcr"` | GHCR auth ID for check 14 |
| `RUNPOD_REGISTRY_AUTH_ID_GITLAB` | `"placeholder-gitlab"` | GitLab auth ID for check 14 |
| `RUNPOD_REGISTRY_AUTH_ID_DOCKER_HUB` | `None` | Docker Hub auth ID for check 14 |
| `RUNPOD_REGISTRY_AUTH_ID` | used as fallback for GHCR | Legacy/global auth ID |

### Environment variables read by `CapabilityAuditService`

| Variable | Default | Purpose |
|----------|---------|---------|
| `PITWALL_MONTHLY_BUDGET_USD` | not set | Monthly budget cap for budget headroom check |
| `PITWALL_PER_REQUEST_MAX_USD` | not set | Per-request cost cap for estimate comparison |

### Hardcoded constants in `sixteen_check`

| Name | Value | Used in |
|------|-------|---------|
| `SYNC_RESULT_RETENTION_S` | `60` | check 8 |
| `ASYNC_RESULT_RETENTION_S` | `1800` | check 8 |
| `REQUIRED_DISK_GB_BY_WORKLOAD` | `{"vllm": 80, "embed": 40, "slim": 20}` | check 12 |
| `REQUIRED_REGISTRY_PREFIXES` | `(GHCR_PREFIX, GITLAB_REGISTRY_PREFIX, DOCKER_HUB_PREFIX)` | check 14 |
| `KILL_SWITCH_STEPS` | `("list_pods", "terminate_all", "verify")` | check 16 |
| `DEPRECATED_HF_CLI_COMMAND` | legacy Hugging Face CLI invocation | check 12 |
| `REQUIRED_HF_CLI_COMMAND` | `"hf download"` | check 12 |
| `MAX_VOLUME_ATTACH_TIMEOUT_S` | `300` | check 9 |
| `R2_TEMP_CREDENTIAL_ROUTE_FRAGMENT` | `"temp-access-credentials"` | check 11 route-fragment source check (`src/pitwall/audit/sixteen_check.py:56`, `src/pitwall/audit/sixteen_check.py:744`) |
| `R2_FORBIDDEN_POD_ENV_KEYS` | frozenset of 6 key names | check 11 provider fixture env validation (`src/pitwall/audit/sixteen_check.py:57`, `src/pitwall/audit/sixteen_check.py:797`) |
| `R2_TEMP_CREDENTIAL_STRATEGIES` | frozenset of 9 strategy names | check 11 provider fixture R2 strategy validation (`src/pitwall/audit/sixteen_check.py:67`, `src/pitwall/audit/sixteen_check.py:808`) |
| `POD_LEASE_REQUIRED_READINESS_SIGNALS` | `("runtime", "port_mappings", "probe_2xx")` | check 3 |
| `EXPECTED_AUDIT_CHECK_COUNT` | `19` | registry assertion, capability audit harness-count check |
| `_PRE_SPEND_FINDING_LIMIT` | `32` | maximum structured findings retained per scanner call |
| `_REDACTED_SECRET`, `_REDACTED_PRIVATE_KEY`, `_REDACTED_EMAIL` | redaction markers | `scan_pre_spend_payload` |

---

## 6. Failure modes & error types

### `CheckFailed`

Raised by any of the 19 check functions when an invariant is violated.

```python
class CheckFailed(Exception):
    check_id: int
    message: str
    severity: AuditSeverity
    evidence: str
    remediation: str
```

All 19 checks return `CheckResult` entries through `run_all_checks`. Numeric parsing checks catch `TypeError` and `ValueError` from `int()`/`float()` parses and re-raise as `CheckFailed` with an appropriate `check_id`. Missing config fields raise `CheckFailed` (not `KeyError` or `AttributeError`).

### Pre-spend payload guardrail failure modes

- `scan_pre_spend_payload` returns `decision="block"` for secret/API-token/private-key findings. Runtime routes raise `PreSpendPayloadRejected` and do not call resolver, budget gate, or RunPod clients.
- `scan_pre_spend_payload` returns `decision="redact"` for PII-only findings such as emails. Runtime routes continue with `redacted_payload`; capability audit cost estimation also uses `redacted_payload`.
- Structured scanner output includes `kind`, `rule`, `path`, `action`, `redacted_preview`, and `fingerprint_sha256`. It does not include raw matched PII or secret text.

### `CapabilityAuditService` failure modes

- If the capability does not exist, all 8 checks are still returned; the `capability_exists` check fails with `passed=False`.
- If `PITWALL_MONTHLY_BUDGET_USD` or `PITWALL_PER_REQUEST_MAX_USD` are not set, the corresponding checks return `passed=False` with no exception raised.
- If the provider cost estimator raises `ValueError`, it is caught and appended to `estimate_warnings`; no check fails solely on estimator warning.
- If the payload guardrail blocks, cost estimation is skipped and `cost_estimate_under_cap` returns `passed=False` with a guardrail warning instead of inspecting the raw payload.
- If `budget_state` is provided at construction time, env var resolution is skipped entirely.

### REST route failures

- `RuntimeError` with message "app.state.pool is not configured" if asyncpg pool is not attached to `app.state` (lines 23–26, 33–36, 43–46).
- `PreSpendPayloadRejected` maps `POST /v1/inference` secret/private-key findings to HTTP 422 with redacted finding metadata.
- Returns HTTP 422 if repository `get_by_name` raises (not currently handled; propagates as 500).

---

## 7. Testing

### Test files covering this subsystem

| Path | What it covers |
|------|----------------|
| `tests/test_audit_sixteen_check.py` (1252 lines) | Full hermetic test suite for all 19 checks. `DictAuditConfig` test double. Tests each check's pass/fail paths, pre-spend scanner decisions, CLI exit code tests with `--strict`, and JSON output tests. |
| `tests/audit/test_capability_audit.py` (298 lines) | `CapabilityAuditService` unit tests. FakeCapabilityRepo, FakeProviderRepo. All 8 check paths, payload guardrail block/redact behavior, budget_state, estimate warnings, 19-check integration. |
| `tests/policy/test_engine.py` | Policy schema, YAML loading, evaluator allow/deny behavior, deterministic violation order, default packaged policy examples. |
| `tests/property/test_policy_properties.py` | Hypothesis coverage for `contains_none` provider-target evaluation. |
| `tests/audit/test_rest_audit_mode.py` (137 lines) | FastAPI route integration tests for `POST /v1/admin/audit-capability/{name}`. Monkeypatched audit delegate. Happy path, forced failures, unknown capability. |
| `tests/api/test_inference_contract.py` (235 lines) | REST inference contract tests, including pre-spend secret rejection before resolver/gate and PII redaction before RunPod call arguments. |
| `tests/security/test_pre_spend_payload_guardrails.py` (51 lines) | Hypothesis property tests that email and token redaction never return the original sensitive value in structured scanner output or redacted payloads. |
| `tests/test_provider_audit_evidence.py` | Evidence collection for provider audit. |
| `tests/test_audit_sixteen_check.py::TestStrictModeExitCode` | Verifies `--strict` exit code 0 on all-pass, exit code 1 on any failure, exit code 0 in non-strict even on failure. |
| `tests/test_audit_sixteen_check.py::TestCLI` | Subprocess tests for CLI, JSON output, `--help`. |
| `tests/audit/test_capability_audit.py::test_sixteen_check_results_are_exposed_in_named_status` | Verifies 19-check results are nested inside the legacy-named `16_check_runpod_audit_passed` check's `details` dict. |
| `tests/test_audit_sixteen_check.py::TestSixteenCheckCoverageMatrix` | Enforces one test class per check, pass+fail coverage per class, exact check-id-to-description mapping. |

---

## 8. Dependencies

### From the rest of pitwall

| Import | Source module | Used by |
|--------|---------------|---------|
| `pods`, `templates` | `pitwall.runpod_client` | check functions, source inspection |
| `CANONICAL_GPU_NAMES` | `pitwall.runpod_client.gpu` | check 1 |
| `DOCKER_HUB_PREFIX`, `GHCR_PREFIX`, `GITLAB_REGISTRY_PREFIX`, `registry_auth_id_from_env` | `pitwall.runpod_client.registry` | checks 2, 14 |
| `Capability`, `Provider` | `pitwall.core.models` | `capability.py` |
| `EstimatePayload`, `get_estimator` | `pitwall.cost.estimator` | `capability.py` |
| `BudgetGate` (referenced in docstring) | `pitwall.cost.budget_gate` | `capability.py` |
| `CapabilityRepository`, `ProviderRepository` | `pitwall.db.repository` | REST route |
| `CloudflareR2TempCredentialClient`, `R2TemporaryCredentials` | `pitwall.r2_temp_credentials` | check 11 |
| `CloudflareR2StagingStore`, `NoOpStagingStore`, `get_staging_store` | `pitwall.staging_store` | check 11 (`src/pitwall/audit/sixteen_check.py:737`) |
| `SSH_LOCALHOST_PROBE_METHOD`, `POD_READINESS_PROBE_ORDER`, `_pod_has_runtime`, `_ReadinessSignals`, `DEFAULT_VOLUME_ATTACH_TIMEOUT_S`, `create_pod_with_fallback_sync`, `wait_for_pod_runtime_sync`, `terminate_pod_sync`, `_rest_request`, `RunPodError`, `RunPodRestError` | `pitwall.runpod_client.pods` | checks 3, 4, 9, 15 |
| `lease_launch` (module) | `pitwall.api.leases` | checks 9, 11 |
| `lease_routes` (`router`, `stop_lease`, `patch_lease`) | `pitwall.api.routes.leases` | checks 15, 16 |
| `emergency` (`router`, `run_kill`, `activate_kill_switch`) | `pitwall.api.admin` | check 15 |
| `LeasePatch`, `lease_patch_conflicting_fields` | `pitwall.api.schemas.leases` | check 16 |
| `webhook_receiver` (`app`) | `pitwall.webhook_receiver` | check 7 |

### External libraries

| Library | Version/file | Used by |
|---------|--------------|---------|
| `fastapi` | `APIRouter`, `Depends`, `Request` | REST route |
| `httpx` | `AsyncClient`, `ASGITransport` | test client |
| `asyncpg` | `pool.acquire().fetchrow()` | `read_month_to_date_spend_usd` |
| `decimal.Decimal` | stdlib | all cost/estimate fields |
| `datetime` | stdlib | timestamps in capability service |
| `inspect` | stdlib | source token inspection in checks 4, 9, 11, 13, 15, 16 |
| `json`, `math`, `sys` | stdlib | `sixteen_check.main` |
| `hashlib`, `re` | stdlib | pre-spend payload fingerprinting and deterministic pattern matching |
| `pathlib.Path` | stdlib | `WORKER_VLLM_ENTRYPOINT` path resolution |
| `pytest` | test framework | all test files |
