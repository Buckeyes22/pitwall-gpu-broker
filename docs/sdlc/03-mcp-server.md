# MCP Server & Tools Subsystem

## 1. Purpose & Scope

The MCP server (`pitwall.mcp`) is a standalone FastMCP-based server that exposes 23 named tools to MCP clients (e.g., Claude, Cursor, Codex). It translates MCP tool calls into Pitwall business-logic operations, bridging the MCP wire protocol to the service layer (repositories, inference engine, lease management).

The server runs as `python -m pitwall.mcp` over local stdio. The public alpha rejects SSE and
streamable-HTTP because it does not yet implement MCP HTTP authentication. The server accesses the
same database and domain services as REST handlers without routing calls through the REST API.

## 2. Components

### `pitwall.mcp` — Package init & server bootstrap

**File:** `src/pitwall/mcp/__init__.py`

Creates the `FastMCP("pitwall")` instance and registers all tools via `register_all`. Exposes the `ensure_runtime_env()` guard so the stdio entrypoint can validate required env vars at startup (not at import time, keeping `import pitwall.mcp` hermetic for test collection).

Defines `@mcp.tool() pitwall_health()` returning `{"ok": "true", "backend": "runpod"}`.

**Invariant:** All 23 tools are registered before the server starts. The server must not start if `require_runtime_env("mcp")` fails.

### `pitwall.mcp.__main__` — Transport entrypoint

**File:** `src/pitwall/mcp/__main__.py`

`main()` reads `PITWALL_MCP_TRANSPORT` (default `"stdio"`). Any other value raises `SystemExit`
with an explicit security-boundary message before the MCP server starts. The accepted path calls
`mcp.run(transport="stdio")`.

**Signature:** `def main() -> None`

### `pitwall.mcp.registry` — Tool registry

**File:** `src/pitwall/mcp/registry.py`

Defines `TOOL_NAMES: frozenset[str]` (23 names) and `TOOL_REGISTRY: list[ToolSpec]`.

`ToolSpec` is a frozen dataclass:
```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., dict[str, Any]] | Callable[..., Awaitable[dict[str, Any]]]
```

`register_all(server: Any) -> None` iterates `TOOL_REGISTRY` and calls `server.tool(name=spec.name, description=spec.description)(spec.handler)` for each.

**Invariant:** `len(TOOL_REGISTRY) == 23`, and `set(ToolSpec.name for ToolSpec in TOOL_REGISTRY) == TOOL_NAMES`.

### `pitwall.mcp.schema_adapter` — Pydantic → JSON Schema

**File:** `src/pitwall/mcp/schema_adapter.py`

`pydantic_to_mcp_schema(model_cls: type[BaseModel]) -> dict[str, Any]`

Calls `model_cls.model_json_schema(by_alias=True)`, then:
1. Pops the `$defs` section and resolves all `$ref` pointers by inlining definitions.
2. Strips Pydantic-internal noise keys (`title`, `additionalProperties`).
3. Preserves `type`, `properties`, `required`, descriptions, constraints, defaults, and enum values.

Helper: `_resolve_refs(node: Any, defs: dict[str, Any]) -> Any` (recursive `$ref` resolution with deep-copy to avoid aliasing).
Helper: `_strip_keys(node: Any, strip: frozenset[str], keep_at_top: frozenset[str] | None, depth: int) -> Any`.

### `pitwall.mcp.error_adapter` — Exception mapping

**File:** `src/pitwall/mcp/error_adapter.py`

`adapt_error(exc: Exception) -> McpError`

Extracts error data from service exceptions using `to_response_body()` → `to_dict()` → `error_code` attribute → fallback `"internal_error"`. Wraps it in `McpError(ErrorData(code=-32000, message=..., data=...))`. Uses `register_error_code(error_code: str, mcp_code: int)` to maintain a `dict[str, int]` for future per-code mapping.

**Invariant:** All pitwall service errors map to MCP code `-32000` by default.

### `pitwall.mcp.tools.admin` — Capability & provider write tools

**File:** `src/pitwall/mcp/tools/admin.py`

Implements 6 admin tools. All use `_capability_to_response` / `_provider_to_response` helpers to shape responses, call `insert_audit(pool, actor="mcp:admin", ...)` for every mutation, and raise domain exceptions.

| Function | Signature |
|---|---|
| `pitwall_create_capability` | `(name: str, version: str, capability_class: str, cost_mode: str, description: str \| None = None, input_schema: dict[str, Any] \| None = None, output_schema: dict[str, Any] \| None = None) -> dict[str, Any]` |
| `pitwall_update_capability` | `(capability_id: str, name: str \| None = None, version: str \| None = None, description: str \| None = None, cost_mode: str \| None = None, enabled: bool \| None = None, input_schema: dict[str, Any] \| None = None, output_schema: dict[str, Any] \| None = None) -> dict[str, Any]` |
| `pitwall_create_provider` | `(capability_id: str, name: str, provider_type: str, runpod_endpoint_id: str \| None = None, runpod_template_id: str \| None = None, region: str \| None = None, cloud_type: str \| None = None, config: dict[str, Any] \| None = None, priority: int = 0, enabled: bool = True) -> dict[str, Any]` |
| `pitwall_update_provider` | `(provider_id: str, name: str \| None = None, provider_type: str \| None = None, runpod_endpoint_id: str \| None = None, runpod_template_id: str \| None = None, region: str \| None = None, cloud_type: str \| None = None, config: dict[str, Any] \| None = None, priority: int \| None = None, enabled: bool \| None = None, health_status: str \| None = None, consecutive_failures: int \| None = None, cooldown_trips: int \| None = None, cold_start_p50_ms: int \| None = None, cold_start_p95_ms: int \| None = None, recent_error_rate: float \| None = None) -> dict[str, Any]` |
| `pitwall_disable_provider` | `(provider_id: str) -> dict[str, Any]` |
| `pitwall_hibernate_provider` | `(provider_id: str) -> dict[str, Any]` |

All six are async and raise `CapabilityConflict`, `CapabilityNotFound`, `ProviderConflict`, or `ProviderNotFound` from `pitwall.api.exceptions`.

### `pitwall.mcp.tools.discovery` — Read-only capability/provider tools

**File:** `src/pitwall/mcp/tools/discovery.py`

Implements 4 discovery tools. All use `_capability_to_response` and `_provider_to_response` helpers.

| Function | Signature |
|---|---|
| `pitwall_list_capabilities` | `(capability_class: str \| None = None, cost_mode: str \| None = None, enabled: bool \| None = None) -> dict[str, Any]` |
| `pitwall_describe_capability` | `(name: str) -> dict[str, Any]` |
| `pitwall_list_providers` | `(capability_id: str \| None = None, provider_type: str \| None = None, enabled: bool \| None = None) -> dict[str, Any]` |
| `pitwall_get_provider_health` | `(provider_id: str) -> dict[str, Any]` |

Helpers `_parse_capability_class(value: str | None) -> CapabilityClass | None` and `_parse_provider_type(value: str | None) -> ProviderType | None` catch `ValueError` and return `None`. Raises `CapabilityNotFound` / `ProviderNotFound` from `pitwall.api.exceptions`.

### `pitwall.mcp.tools.inference` — Inference submission & job management

**File:** `src/pitwall/mcp/tools/inference.py`

Implements 5 inference tools. Handles idempotency replay via `_replay_idempotent_inference`, which queries `pitwall.workloads` by `idempotency_key` and verifies canonical JSON of the original input matches the new request. Raises `IdempotencyMismatch` from `pitwall.api.exceptions` on mismatch.

Control fields stripped from capability params: `_CONTROL_FIELDS = {"capability_id", "capability", "capability_name", "provider_id", "dry_run", "idempotency_key"}`; `_JOB_CONTROL_FIELDS` additionally includes `"webhook_url"`.

| Function | Signature |
|---|---|
| `pitwall_submit_inference` | `(capability_id: str, provider_id: str \| None = None, dry_run: bool = False, idempotency_key: str \| None = None, **kwargs: Any) -> dict[str, Any]` |
| `pitwall_submit_job` | `(capability_id: str, input: dict[str, Any], provider_id: str \| None = None, dry_run: bool = False, idempotency_key: str \| None = None, webhook_url: str \| None = None, **kwargs: Any) -> dict[str, Any]` |
| `pitwall_get_job_status` | `(workload_id: str) -> dict[str, Any]` |
| `pitwall_get_job_result` | `(workload_id: str) -> dict[str, Any]` |
| `pitwall_cancel_job` | `(workload_id: str) -> dict[str, Any]` |

All delegate to `pitwall.core.inference` (see `resolve_inference_target`, `run_sync_inference`, `create_and_dispatch_job`, `cancel_job`) and use `normalize_workload_output` to shape responses.

### `pitwall.mcp.tools.leases` — Pod lease management

**File:** `src/pitwall/mcp/tools/leases.py`

Implements 4 lease tools. All use `_lease_to_response` to shape responses.

| Function | Signature |
|---|---|
| `pitwall_lease_pod` | `(capability_id: str, provider_id: str \| None = None, dry_run: bool = False, idempotency_key: str \| None = None) -> dict[str, Any]` |
| `pitwall_get_lease` | `(lease_id: str) -> dict[str, Any]` |
| `pitwall_renew_lease` | `(lease_id: str, extends_minutes: int = 60, idempotency_key: str \| None = None) -> dict[str, Any]` |
| `pitwall_stop_lease` | `(lease_id: str, reason: str \| None = None) -> dict[str, Any]` |

`pitwall_lease_pod` calls `run_launch` from `pitwall.api.leases.launch`. `pitwall_stop_lease` calls `run_teardown` from `pitwall.api.leases.teardown`. Renewal uses the same atomic, bounded, audited, optionally idempotent mutation service as REST. Raises `LeaseNotFound` / `ProviderUnavailable`.

### `pitwall.mcp.tools.cost` — Cost reporting

**File:** `src/pitwall/mcp/tools/cost.py`

| Function | Signature |
|---|---|
| `pitwall_cost_summary` | `(capability_class: str \| None = None, since: str \| None = None, until: str \| None = None) -> dict[str, Any]` |
| `pitwall_recent_workloads` | `(limit: int = 20, state: str \| None = None, capability_id: str \| None = None, provider_id: str \| None = None, provider_type: str \| None = None, since: str \| None = None, until: str \| None = None) -> dict[str, Any]` |

Both delegate to `pitwall.core.cost_reporting` (respectively `cost_summary` and `recent_workloads` service functions). Date strings are parsed via `_parse_date` (ISO format) and `_parse_datetime` (ISO format, UTC).

### `pitwall.mcp.tools.audit` — Audit log read

**File:** `src/pitwall/mcp/tools/audit.py`

| Function | Signature |
|---|---|
| `pitwall_audit_log` | `(entity_type: str \| None = None, entity_id: str \| None = None, action: str \| None = None, limit: int = 50) -> dict[str, Any]` |

Delegates to `pitwall.db.repository.list_audit`. Returns entries with id, actor, action, entity_type, entity_id, old_value, new_value, change_reason, and created_at.

### `pitwall.mcp.tools.copilot` — Broker Copilot proposals

**File:** `src/pitwall/mcp/tools/copilot.py`

| Function | Signature |
|---|---|
| `pitwall_copilot_propose` | `(intent: str, provider_ref: str \| None = None, provider_enabled: bool \| None = None, provider_priority: int \| None = None, provider_patch: dict[str, Any] \| None = None, scorecards: list[dict[str, Any]] \| None = None, drift_findings: list[dict[str, Any]] \| None = None) -> dict[str, Any]` |

Proposal-only operator assistant. It reads current capabilities/providers through the repository layer, translates constrained provider intents (enable, disable, priority, or explicit provider patch) into a GitOps `DesiredState`, calls `pitwall.gitops.build_reconcile_plan`, and returns `desired_state`, `plan`, `diff`, `rationale`, and `recommendations`. It can convert optional scorecard and drift signal snapshots through `RecommendationEngine.recommend(...)` and use supported provider enablement recommendations as proposal input. It never imports or calls `apply_plan` and never mutates repositories.

### `pitwall.mcp.tools.output` — Workload normalization

**File:** `src/pitwall/mcp/tools/output.py`

`normalize_workload_output(workload: Workload) -> dict[str, Any]`

Returns a consistent dict:
```python
{
    "workload_id": workload.id,
    "cost": {"estimate_usd": _decimal_to_str(workload.cost_estimate_usd), "actual_usd": _decimal_to_str(workload.cost_actual_usd)},
    "provider_id": workload.provider_id,
    "state": workload.state.value if hasattr(workload.state, "value") else workload.state,
    "result": workload.result,
    "trace_id": workload.langfuse_trace_id,
}
```

Used by all inference and job tools. Decimal fields are serialized as strings.

## 3. Tool Inventory

| Tool Name | Description | Inputs | Outputs |
|---|---|---|---|
| `pitwall_list_capabilities` | List registered capabilities | `capability_class`, `cost_mode`, `enabled` | `{capabilities: [...]}` |
| `pitwall_describe_capability` | Single capability details | `name` | capability dict |
| `pitwall_list_providers` | List registered providers | `capability_id`, `provider_type`, `enabled` | `{providers: [...]}` |
| `pitwall_get_provider_health` | Provider health & cooldown | `provider_id` | health dict |
| `pitwall_submit_inference` | Sync inference request | `capability_id`, `provider_id`, `dry_run`, `idempotency_key`, `**kwargs` | workload dict |
| `pitwall_submit_job` | Async job submission | `capability_id`, `input`, `provider_id`, `dry_run`, `idempotency_key`, `webhook_url`, `**kwargs` | workload dict |
| `pitwall_get_job_status` | Async job state | `workload_id` | workload dict |
| `pitwall_get_job_result` | Async job result | `workload_id` | workload dict |
| `pitwall_cancel_job` | Cancel async job | `workload_id` | workload dict with `cancelled` flag |
| `pitwall_lease_pod` | Create pod lease | `capability_id`, `provider_id`, `dry_run`, `idempotency_key` | lease dict |
| `pitwall_get_lease` | Lease details | `lease_id` | lease dict |
| `pitwall_renew_lease` | Extend lease | `lease_id`, `extends_minutes`, `idempotency_key` | lease dict |
| `pitwall_stop_lease` | Tear down lease | `lease_id`, `reason` | lease dict |
| `pitwall_cost_summary` | Aggregated cost | `capability_class`, `since`, `until` | `{total_usd, entries}` |
| `pitwall_recent_workloads` | Recent workload list | `limit`, `state`, `capability_id`, `provider_id`, `provider_type`, `since`, `until` | `{workloads}` |
| `pitwall_create_capability` | Register capability | `name`, `version`, `capability_class`, `cost_mode`, `description`, `input_schema`, `output_schema` | capability dict |
| `pitwall_update_capability` | Update capability | `capability_id`, `name`, `version`, `description`, `cost_mode`, `enabled`, `input_schema`, `output_schema` | capability dict |
| `pitwall_create_provider` | Register provider | `capability_id`, `name`, `provider_type`, `runpod_endpoint_id`, `runpod_template_id`, `region`, `cloud_type`, `config`, `priority`, `enabled` | provider dict |
| `pitwall_update_provider` | Update provider | `provider_id`, many optional fields | provider dict |
| `pitwall_disable_provider` | Disable provider | `provider_id` | provider dict |
| `pitwall_hibernate_provider` | Hibernate provider | `provider_id` | provider dict |
| `pitwall_audit_log` | Config audit trail | `entity_type`, `entity_id`, `action`, `limit` | `{entries}` |
| `pitwall_copilot_propose` | Proposal-only GitOps copilot | `intent`, optional provider patch fields, optional scorecard/drift signals | `{proposal_only, applied, desired_state, plan, diff, rationale, recommendations}` |
| `pitwall_health` | Server health | (none) | `{ok, backend}` |

**Service-layer mapping:** Discovery tools → `CapabilityRepository` / `ProviderRepository`. Inference tools → `pitwall.core.inference` functions. Admin tools → `CapabilityRepository` / `ProviderRepository` + `insert_audit`. Lease tools → `LeaseRepository` + `run_launch` / `run_teardown`. Cost tools → `pitwall.core.cost_reporting`. Audit tool → `list_audit`. Copilot tool → `CapabilityRepository` / `ProviderRepository` + `pitwall.gitops.build_reconcile_plan` + optional `RecommendationEngine`.

## 4. Public Interfaces

Functions and classes callable from other subsystems:

| Symbol | Module | Signature |
|---|---|---|
| `mcp` | `pitwall.mcp` | `FastMCP` instance (run target) |
| `ensure_runtime_env` | `pitwall.mcp` | `() -> None` |
| `TOOL_NAMES` | `pitwall.mcp.registry` | `frozenset[str]` |
| `TOOL_REGISTRY` | `pitwall.mcp.registry` | `list[ToolSpec]` |
| `ToolSpec` | `pitwall.mcp.registry` | `@dataclass frozen` |
| `register_all` | `pitwall.mcp.registry` | `(server: Any) -> None` |
| `pydantic_to_mcp_schema` | `pitwall.mcp.schema_adapter` | `(model_cls: type[BaseModel]) -> dict[str, Any]` |
| `adapt_error` | `pitwall.mcp.error_adapter` | `(exc: Exception) -> McpError` |
| `register_error_code` | `pitwall.mcp.error_adapter` | `(error_code: str, mcp_code: int) -> None` |
| `PITWALL_ERROR_CODE_BASE` | `pitwall.mcp.error_adapter` | `int` (= -32000) |
| `normalize_workload_output` | `pitwall.mcp.tools.output` | `(workload: Workload) -> dict[str, Any]` |

## 5. Configuration

Environment variables read by the MCP subsystem:

| Env Var | Default | Type | Description |
|---|---|---|---|
| `PITWALL_MCP_TRANSPORT` | `"stdio"` | `Literal["stdio"]` | Local transport; every other value is rejected |

Required runtime env for the MCP service (validated by `require_runtime_env("mcp")`):
- `RUNPOD_API_KEY`
- `DATABASE_URL`
- `REDIS_URL`

The transport setting is also validated as `Literal["stdio"]` by `PitwallSettings`; the process
entry point reads it directly so it can fail before serving.

## 6. Failure Modes & Error Types

**Startup errors:**
- `SystemExit(os.EX_CONFIG)` — raised by `require_runtime_env()` if `RUNPOD_API_KEY`, `DATABASE_URL`, or `REDIS_URL` is missing or whitespace-empty.
- `SystemExit` — raised by `main()` if `PITWALL_MCP_TRANSPORT` is anything other than `stdio`.

**Runtime exceptions raised by tools:**

`pitwall.api.exceptions.PitwallApiError` and its subclasses, all with `error_code` class attributes:

| Class | error_code | Raised by |
|---|---|---|
| `CapabilityNotFound` | `"capability_not_found"` | discovery, admin, inference tools |
| `CapabilityDisabled` | `"capability_disabled"` | inference tools |
| `CapabilityConflict` | `"capability_conflict"` | admin create tools |
| `ProviderNotFound` | `"provider_not_found"` | discovery, admin, leases, inference tools |
| `ProviderUnavailable` | `"no_providers_available"` | inference, leases tools |
| `ProviderConflict` | `"provider_conflict"` | admin create tools |
| `LeaseNotFound` | `"lease_not_found"` | leases tools |
| `LeaseStateConflict` | `"lease_state_conflict"` | lease renewal/stop |
| `IdempotencyMismatch` | `"idempotency_mismatch"` | inference tools |
| `WorkloadNotFound` | `"workload_not_found"` | job status/result/cancel |
| `JobNotReady` | `"job_not_ready"` | job result |
| `ChangeSetTooBroad` | `"change_set_too_broad"` | admin update |
| `RateLimited` | `"rate_limited"` | inference (RunPod) |

`ValueError` — raised directly in admin tools when `capability_class`, `cost_mode`, or `provider_type` has an unrecognized enum value.

**Error adapter:** `adapt_error()` converts every `PitwallApiError` subclass to an `McpError` with `ErrorData(code=-32000, message=<str(exc)>, data=<to_response_body()>)`. The `data["error"]` field carries the same string code the REST API would return.

**Idempotency edge case:** If an `idempotency_key` is provided but the canonical JSON of the new capability params differs from the original submission stored in `pitwall.workloads.idempotency_key`, `IdempotencyMismatch` is raised with the replayed workload ID.

**Dry run:** Every submit/lease tool returns a synthetic response with `dry_run: True`, `state: "completed"` or `"queued"`, and no real Workload record is created.

## 7. Testing

| File / Path | What it covers |
|---|---|
| `tests/fakes/mcp.py` | `FakeServiceLayerRecorder` — wraps `TOOL_REGISTRY` handlers to record calls, results, and errors for hermetic contract testing. Provides `install()`/`uninstall()` to patch/restore `ToolSpec.handler` at runtime, `call_tool()`, `get_calls()`, `assert_called()`, and `reset()`. |
| `tests/mcp/test_stdio_transport.py` | Starts the installed-style stdio subprocess and validates MCP initialization and tool discovery. |
| `tests/mcp/test_entrypoint.py` | Proves stdio dispatch and fail-closed rejection of every network transport value. |
| `tests/mcp/test_tool_contract.py` | Validates registered names, schemas, behavior, and error adaptation. |

The public-alpha suite intentionally contains no SSE server fixture: network MCP is outside the
supported and authenticated surface.

## 8. Dependencies

**Internal imports (Pitwall):**

| Source module | What's imported | Used in |
|---|---|---|
| `pitwall.config` | `require_runtime_env`, `load_settings_from_env` | `__init__.py`, `inference.py` |
| `pitwall.core.enums` | `CapabilityClass`, `CapabilitySource`, `CostMode`, `ProviderType`, `ResultDelivery` | `admin.py`, `discovery.py` |
| `pitwall.core.models` | `Capability`, `Provider`, `Workload`, `CapabilityDefaults` | `admin.py`, `discovery.py`, `output.py` |
| `pitwall.core.inference` | `resolve_inference_target`, `run_sync_inference`, `create_and_dispatch_job`, `cancel_job` | `inference.py` |
| `pitwall.core.cost_reporting` | `cost_summary`, `recent_workloads` service functions | `cost.py` |
| `pitwall.db` | `get_pool` | all tool modules |
| `pitwall.db.repository` | `CapabilityRepository`, `ProviderRepository`, `LeaseRepository`, `WorkloadRepository`, `insert_audit`, `list_audit` | `admin.py`, `discovery.py`, `inference.py`, `leases.py`, `audit.py` |
| `pitwall.resolver` | `CapabilityDisabledError`, `CapabilityNotFoundError`, `NoHealthyProviderError`, `ProviderNotFoundError` | `inference.py` |
| `pitwall.api.exceptions` | `PitwallApiError` subclasses | all tool modules |
| `pitwall.api.provider_schemas` | `validate_provider_registration_config` | `admin.py` |
| `pitwall.api.leases.launch` | `run_launch` | `leases.py` |
| `pitwall.api.leases.teardown` | `run_teardown` | `leases.py` |
| `pitwall.core.ids` | `ulid_new` | `admin.py` |

**External dependencies:**

| Library | Version/Source | Used for |
|---|---|---|
| `mcp` (SDK) | `mcp.server.fastmcp.FastMCP` | Server bootstrap, `@mcp.tool()` decorator |
| `pydantic` | `BaseModel` | `schema_adapter.pydantic_to_mcp_schema` |
