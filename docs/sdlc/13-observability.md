# Observability & Cost Export — SDLC 13

## 1. Purpose & Scope

This subsystem covers two loosely coupled concerns:

- **Observability / tracing** — structured trace emission for inference requests through the `Trace` / `Tracer` Protocol seam; Langfuse is the optional vendor backend (`src/pitwall/observability/langfuse.py:62`, `src/pitwall/observability/langfuse.py:83`, `pyproject.toml:61`, `pyproject.toml:62`).
- **Cost export** — a Prometheus-style HTTP metrics endpoint that surfaces cloud spend, budget utilization, active lease counts, kill-switch triggers, and provider health, sourced from Postgres (`src/pitwall/cost/exporter.py`).

The subsystem sits at the "edge" of Pitwall: the tracer seam is called by inference request handlers, while the cost exporter is a standalone FastAPI process scraped by Prometheus.

---

## 2. Components

### `src/pitwall/observability/langfuse.py`

**Responsibility:** Define the tracing Protocol seam for synchronous inference requests and provide the default Langfuse-backed implementation (`src/pitwall/observability/langfuse.py:62`, `src/pitwall/observability/langfuse.py:83`, `src/pitwall/observability/langfuse.py:188`). Traces capture workload metadata, provider, input/output bytes, duration, and error information. Failures in tracing emission are siloed — they never propagate to callers (`src/pitwall/observability/langfuse.py:137`, `src/pitwall/observability/langfuse.py:185`, `src/pitwall/observability/langfuse.py:234`).

**Key classes/functions:**

```python
@runtime_checkable
class Trace(Protocol):
    @property
    def trace_id(self) -> str | None: ...
    def event(self, name: str, **kwargs: Any) -> None: ...
    def finish(...) -> None: ...
```

Active trace handle exposed to inference call sites. `InferenceTrace` is the current concrete `Trace` implementation; when it wraps `None`, every public method is a no-op (`src/pitwall/observability/langfuse.py:62`, `src/pitwall/observability/langfuse.py:115`, `src/pitwall/observability/langfuse.py:130`, `src/pitwall/observability/langfuse.py:149`).

```python
@runtime_checkable
class Tracer(Protocol):
    def start_inference_trace(...) -> Trace: ...
    def emit_inference_trace(...) -> str | None: ...
```

Backend contract used by inference and API call sites. The module default is `LangfuseTracer()` behind `_default_tracer`; `get_tracer()` returns that `Tracer` (`src/pitwall/observability/langfuse.py:83`, `src/pitwall/observability/langfuse.py:275`, `src/pitwall/observability/langfuse.py:278`).

```python
class LangfuseTracer:
    def start_inference_trace(...) -> InferenceTrace: ...
    def emit_inference_trace(...) -> str | None: ...
```

The selected concrete implementation. It calls `_get_client()` lazily; if the Langfuse client is unavailable, it returns `InferenceTrace(None, {}, started_at)` as a no-op `Trace` (`src/pitwall/observability/langfuse.py:188`, `src/pitwall/observability/langfuse.py:200`, `src/pitwall/observability/langfuse.py:201`, `src/pitwall/observability/langfuse.py:202`).

```python
def _get_client() -> Any | None
```

Lazy-initializes the Langfuse client. Returns `None` if `langfuse_public_key` or `langfuse_secret_key` are not set in settings. Imports `langfuse` inside the function, so a base install without the `tracing` extra can import and run; initialization failures log `langfuse_client_init_failed` and return `None` (`src/pitwall/observability/langfuse.py:26`, `src/pitwall/observability/langfuse.py:27`, `src/pitwall/observability/langfuse.py:30`, `src/pitwall/observability/langfuse.py:40`, `pyproject.toml:61`, `pyproject.toml:62`).

```python
def start_inference_trace(
    workload_id: str,
    capability_name: str,
    provider_id: str,
    provider_type: str,
    runpod_endpoint_id: str | None = None,
    cost_estimate_usd: float | None = None,
    input_bytes: int | None = None,
    **extra_tags: Any,
) -> Trace
```

Creates a trace through the default `Tracer`. With the default `LangfuseTracer`, a configured Langfuse client receives tags `["pitwall", capability_name, provider_type]`; unavailable Langfuse returns a no-op `Trace`. Caller must invoke `.finish()` (`src/pitwall/observability/langfuse.py:212`, `src/pitwall/observability/langfuse.py:282`, `src/pitwall/observability/langfuse.py:292`).

```python
def emit_inference_trace(
    workload_id: str,
    capability_name: str,
    provider_id: str,
    provider_type: str,
    runpod_endpoint_id: str | None = None,
    cost_estimate_usd: float | None = None,
    input_bytes: int | None = None,
    output_bytes: int | None = None,
    execution_ms: int | None = None,
    status: str = "success",
    error: BaseException | None = None,
) -> str | None
```

Convenience that calls `start_inference_trace` on the default `Tracer` then immediately `.finish()`. Returns `trace.trace_id` (which may be `None` when tracing is a no-op) (`src/pitwall/observability/langfuse.py:242`, `src/pitwall/observability/langfuse.py:256`, `src/pitwall/observability/langfuse.py:265`, `src/pitwall/observability/langfuse.py:272`).

```python
class InferenceTrace:
    def __init__(self, lf_trace: Any | None, metadata: dict[str, Any], started_at: float): ...
    @property
    def trace_id(self) -> str | None: ...
    def event(self, name: str, **kwargs: Any) -> None: ...
    def finish(
        self,
        *,
        status: str,
        execution_ms: int | None = None,
        error: BaseException | None = None,
        input_bytes: int | None = None,
        output_bytes: int | None = None,
        **extra_metadata: Any,
    ) -> None: ...
```

Wraps a Langfuse trace object and satisfies the `Trace` Protocol. All public methods are no-ops when `lf_trace is None`. `.finish()` updates the trace with metadata including `duration_ms`, `status`, `error_type`, `error_message`, `input_bytes`, `output_bytes` (`src/pitwall/observability/langfuse.py:115`, `src/pitwall/observability/langfuse.py:149`, `src/pitwall/observability/langfuse.py:160`, `src/pitwall/observability/langfuse.py:162`, `src/pitwall/observability/langfuse.py:165`, `src/pitwall/observability/langfuse.py:166`, `src/pitwall/observability/langfuse.py:169`, `src/pitwall/observability/langfuse.py:172`).

**Invariant:** Langfuse client initialization and trace emission failures are swallowed at the `log.warning` level and never raise — callers always receive a valid `Trace` (possibly a no-op `InferenceTrace`) (`src/pitwall/observability/langfuse.py:40`, `src/pitwall/observability/langfuse.py:137`, `src/pitwall/observability/langfuse.py:185`, `src/pitwall/observability/langfuse.py:234`).

---

### `src/pitwall/observability/__init__.py`

Re-exports `InferenceTrace`, `LangfuseTracer`, `Trace`, `Tracer`, `emit_inference_trace`, `get_tracer`, `reset_client_for_tests`, and `start_inference_trace` from `langfuse.py`. This is the package public API (`src/pitwall/observability/__init__.py:3`, `src/pitwall/observability/__init__.py:14`).

---

### `src/pitwall/cost/exporter.py` (canonical location)

**Responsibility:** Standalone FastAPI process that polls Postgres every 60 seconds and exposes
cost, provider, queue, reconciliation, webhook-delivery, retention, and kill-switch gauges. The
deprecated shim at `src/pitwall/cost_exporter/` delegates here.

**Key functions:**

```python
async def _refresh(app: FastAPI) -> None
```

Runs on every poll iteration (every 60 seconds). Queries the complete current-month spend and
per-provider spend, active leases, unhealthy providers, kill-switch count, queued workloads, age
of the oldest queued/running workload, due and terminal outbound webhook failures, and the latest
completed retention run. Gauge label sets are cleared before per-provider values are replaced.

Then sets all Gauge values. `active_workers` is `.clear()`-ed first, then re-populated per-provider. `cloud_budget_pct` is `total_spend / budget * 100.0`.

```python
async def _poll_loop(app: FastAPI) -> None
```

Infinite loop: calls `_refresh`, catches all exceptions as `log.exception("refresh failed: %s", exc)`, sleeps 60 s.

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]
```

Entrypoint lifespan manager. Validates `DATABASE_URL` env var is present (exits with code 1 if not). Creates an `asyncpg` pool (`min_size=1, max_size=3`) and stores it in `app.state.pool`. Reads `BUDGET_USD` from env and stores in `app.state.budget`. Sets `cloud_budget_usd`. Starts `_poll_loop` as a background task. On shutdown: cancels the poll task and closes the pool.

```python
@app.get("/metrics")
def metrics() -> Response
```

Returns `generate_latest()` from `prometheus_client` with content type `CONTENT_TYPE_LATEST`.

```python
def main() -> int
```

Loads `PITWALL_COST_EXPORTER_PORT` from env (default `9109`) and calls `uvicorn.run(app, host="0.0.0.0", port=port)`.

**Invariant:** The app will `SystemExit(1)` during startup if `DATABASE_URL` is absent.

---

### `src/pitwall/cost_exporter/` (deprecated shim)

Both `app.py` and `__init__.py` re-export from `pitwall.cost.exporter`. `__main__.py` still serves as the `python -m pitwall.cost_exporter` entry point, which calls `uvicorn.run("pitwall.cost_exporter:app", ...)`. This is a compatibility shim — new code should import from `pitwall.cost.exporter` directly.

---

### `src/pitwall/observability/scorecards.py`

**Responsibility:** Pure analytics module that aggregates windows of cost, latency, and quality observations into normalized per-entity scorecards for routing and governance decisions. No I/O — entirely deterministic given inputs (`src/pitwall/observability/scorecards.py:44`, `src/pitwall/observability/scorecards.py:83`).

**Key classes/functions:**

```python
@dataclass(frozen=True, slots=True)
class ScorecardObservation:
    provider_id: str
    capability_id: str
    cost_usd: Decimal
    latency_ms: float
    quality: float  # 0.0 to 1.0, higher is better
    observed_at: datetime | None = None
```

One raw observation. Quality is caller-defined; `observations_from_workloads` derives it from Workload error presence by default (`src/pitwall/observability/scorecards.py:228`, `src/pitwall/observability/scorecards.py:245`).

```python
@dataclass(frozen=True, slots=True)
class EntityScorecard:
    provider_id: str
    capability_id: str
    cost_usd: Decimal
    latency_ms: float
    quality: float
    cost_normalized: float
    latency_normalized: float
    quality_normalized: float
    composite_score: float
    rank: int
    observation_count: int
    window_start: datetime | None
    window_end: datetime | None
```

Normalized scorecard for one *(provider, capability)* tuple. Each dimension is min-max normalised against the peer group in the observation window: cost and latency are *lower-is-better* (inverted), quality is *higher-is-better*. Composite is a weighted arithmetic or geometric mean (`src/pitwall/observability/scorecards.py:44`, `src/pitwall/observability/scorecards.py:65`).

```python
class ScorecardBuilder:
    def __init__(
        self,
        *,
        cost_weight: float = 1.0,
        latency_weight: float = 1.0,
        quality_weight: float = 1.0,
        cost_aggregator: Literal["mean", "median", "p95", "sum"] = "mean",
        latency_aggregator: Literal["mean", "median", "p95", "sum"] = "mean",
        quality_aggregator: Literal["mean", "median", "p95", "sum"] = "mean",
        composite_method: Literal["arithmetic", "geometric"] = "arithmetic",
    ) -> None: ...

    def build(
        self,
        observations: Sequence[ScorecardObservation],
        *,
        now: datetime | None = None,
    ) -> tuple[EntityScorecard, ...]: ...
```

Groups observations by *(provider_id, capability_id)*, applies per-dimension aggregators, normalises, computes composite scores, and ranks descending. Ties broken by provider_id then capability_id. `now` is reserved for future time-bounded windows (`src/pitwall/observability/scorecards.py:83`, `src/pitwall/observability/scorecards.py:110`).

```python
def observations_from_workloads(
    workloads: Sequence[Any],
    *,
    quality_extractor: Callable[[Any], float] | None = None,
) -> list[ScorecardObservation]
```

Converts Workload-shaped objects into observations. Default quality is `1.0` when `error` is absent/None, else `0.0`. Cost is read from `cost_actual_usd`, latency from `execution_ms`, timestamp from `completed_at` (`src/pitwall/observability/scorecards.py:228`).

**Invariant:** The builder is deterministic: identical observation sequences always produce identical scorecard tuples. Normalised scores are clamped to `[0, 1]`. Geometric composite is `0.0` if any normalised dimension is `0.0`.

---

## 3. Metrics & Exporters

### Prometheus metrics surface

| Metric name | Type | Labels | Source query |
|---|---|---|---|
| `pitwall_cloud_spend_month_usd` | Gauge | — | `SUM(cost_actual_usd)` of workloads in current month, states `queued`/`running`/`completed` |
| `pitwall_cloud_budget_pct` | Gauge | — | `cloud_spend_month_usd / budget * 100` |
| `pitwall_cloud_budget_usd` | Gauge | — | env `PITWALL_MONTHLY_BUDGET_USD` (default `1000`) |
| `pitwall_active_workers` | Gauge | `provider` | `COUNT(leases.id)` JOIN `providers` WHERE `leases.state = 'active'` GROUP BY `providers.name` |
| `pitwall_kill_log_triggers_7d` | Gauge | — | `COUNT(*)` from `kill_log` where `triggered_at > now() - interval '7 days'` |
| `pitwall_providers_unhealthy` | Gauge | — | `COUNT(*)` from `providers` where `health_status = 'unhealthy'` |
| `pitwall_workload_queue_depth` | Gauge | — | queued workload count |
| `pitwall_reconciliation_lag_seconds` | Gauge | — | age of oldest queued/running workload |
| `pitwall_webhook_delivery_retries_due` | Gauge | — | outbound failures whose retry time is due |
| `pitwall_webhook_delivery_terminal_failures_24h` | Gauge | — | terminal outbound failures attempted in 24 hours |
| `pitwall_provider_spend_month_usd` | Gauge | `provider` | current-month actual spend grouped by provider |
| `pitwall_retention_last_success_timestamp_seconds` | Gauge | — | completion time of latest successful retention run |
| `pitwall_retention_last_deleted_count` | Gauge | — | deleted rows recorded by latest successful retention run |

`config/prometheus/pitwall-cloud-alerts.yml` provides operator-tunable examples for budget
thresholds, unhealthy providers, queue backlog, reconciliation lag, overdue and terminal webhook
deliveries, retention staleness, and emergency kill-switch activity. The included thresholds are
safe starting points, not universal service-level objectives; operators should tune them to their
traffic and paging policy.

### Endpoints

- `GET /metrics` — Prometheus scrape target.
- `GET /healthz` / `GET /health` — process-liveness probes.
- `GET /readyz` — dependency readiness; returns 503 unless PostgreSQL is reachable.

### Trace fields

Non-no-op Langfuse traces written per call to `emit_inference_trace` contain:

- **metadata:** `workload_id`, `capability`, `provider_id`, `provider_type`, optional endpoint and
  cost identifiers, byte counts, duration, status, and error **type** only. Raw payloads, exception
  messages, credentials, and authorization values are not exported.
- **tags:** `["pitwall", capability_name, provider_type]` (`src/pitwall/observability/langfuse.py:212`)
- **output:** `{"status": status, "error_type": ...}` (only if error_type is in metadata) (`src/pitwall/observability/langfuse.py:177`, `src/pitwall/observability/langfuse.py:178`)

---

## 4. Public Interfaces

### From `pitwall.observability`

```python
tracer = get_tracer()  # -> Tracer

# Start a trace and get a handle; caller must call .finish()
trace = start_inference_trace(
    workload_id: str,
    capability_name: str,
    provider_id: str,
    provider_type: str,
    runpod_endpoint_id: str | None = None,
    cost_estimate_usd: float | None = None,
    input_bytes: int | None = None,
    **extra_tags: Any,
) -> Trace

# Fire-and-forget convenience: start + finish in one call
trace_id = emit_inference_trace(
    workload_id: str,
    capability_name: str,
    provider_id: str,
    provider_type: str,
    runpod_endpoint_id: str | None = None,
    cost_estimate_usd: float | None = None,
    input_bytes: int | None = None,
    output_bytes: int | None = None,
    execution_ms: int | None = None,
    status: str = "success",
    error: BaseException | None = None,
) -> str | None

# Reset the cached Langfuse client (for test isolation)
reset_client_for_tests() -> None
```

`Trace` and `Tracer` are the public Protocol seam; `LangfuseTracer` is the default concrete implementation (`src/pitwall/observability/langfuse.py:62`, `src/pitwall/observability/langfuse.py:83`, `src/pitwall/observability/langfuse.py:188`, `src/pitwall/observability/__init__.py:6`).

### Scorecards (`pitwall.observability.scorecards`)

```python
from pitwall.observability.scorecards import ScorecardBuilder, ScorecardObservation, observations_from_workloads

# Build scorecards from a window of observations
builder = ScorecardBuilder(
    cost_weight=1.0,
    latency_weight=1.0,
    quality_weight=1.0,
    cost_aggregator="mean",      # or "median", "p95", "sum"
    latency_aggregator="mean",
    quality_aggregator="mean",
    composite_method="arithmetic",  # or "geometric"
)
cards = builder.build([
    ScorecardObservation(
        provider_id="runpod-us-east-1",
        capability_id="embedding.bge-m3",
        cost_usd=Decimal("0.05"),
        latency_ms=120.0,
        quality=1.0,
    ),
    # ... more observations
])

# cards is a tuple[EntityScorecard, ...] ordered by composite_score descending
# Each card carries: cost_usd, latency_ms, quality, *_normalized, composite_score, rank

# Derive observations from Workload records
obs = observations_from_workloads(workloads, quality_extractor=lambda w: 1.0 - w.recent_error_rate)
```

### From `pitwall.cost.exporter`

```python
# FastAPI app instance — run with uvicorn
app: FastAPI

# Module-level gauges (for testing)
cloud_spend_month_usd: Gauge
cloud_budget_pct: Gauge
cloud_budget_usd: Gauge
active_workers: Gauge
kill_log_triggers_7d: Gauge
providers_unhealthy: Gauge

# Internal — exposed for test shims
async def _refresh(app: FastAPI) -> None
async def _poll_loop(app: FastAPI) -> None
```

### Via `pitwall.cost_exporter` shim (deprecated)

```python
from pitwall.cost_exporter import app, _poll_loop, _refresh
```

---

## 5. Configuration

### Observability / tracing

All via `PitwallSettings` / env vars:

| Env var | Setting field | Default | Description |
|---|---|---|---|
| `LANGFUSE_HOST` | `langfuse_host` | `""` (uses Langfuse cloud) | Langfuse server host |
| `LANGFUSE_PUBLIC_KEY` | `langfuse_public_key` | `""` | Public key; if empty, tracing is disabled |
| `LANGFUSE_SECRET_KEY` | `langfuse_secret_key` | `""` | Secret key; if empty, tracing is disabled |

Out-of-the-box installs do not include `langfuse`; the vendor SDK lives behind the `tracing` extra (`pyproject.toml:24`, `pyproject.toml:38`, `pyproject.toml:61`, `pyproject.toml:62`). `_get_client()` imports `Langfuse` lazily and returns `None` when credentials are absent or initialization fails, so tracing degrades to a no-op `Trace` (`src/pitwall/observability/langfuse.py:26`, `src/pitwall/observability/langfuse.py:27`, `src/pitwall/observability/langfuse.py:30`, `src/pitwall/observability/langfuse.py:40`, `src/pitwall/observability/langfuse.py:201`).

When enabled, the Langfuse client is created with `host=settings.langfuse_host or "https://cloud.langfuse.com"`, `flush_interval=5`, `flush_at=20` (`src/pitwall/observability/langfuse.py:32`, `src/pitwall/observability/langfuse.py:35`, `src/pitwall/observability/langfuse.py:36`, `src/pitwall/observability/langfuse.py:37`).

### Cost Exporter

| Env var | Default | Description |
|---|---|---|
| `DATABASE_URL` | **required** | PostgreSQL connection URL |
| `PITWALL_MONTHLY_BUDGET_USD` | `"1000"` | Monthly budget hard cap in USD (fed to `BUDGET_USD` Gauge) |
| `PITWALL_COST_EXPORTER_PORT` | `"9109"` | TCP port the exporter listens on |

`cost-exporter` service requires only `DATABASE_URL` at runtime (`src/pitwall/config.py:545`).

---

## 6. Failure Modes & Error Types

### Tracing

- `_get_client()` returns `None` silently if keys are absent; callers receive a no-op `Trace` (`src/pitwall/observability/langfuse.py:26`, `src/pitwall/observability/langfuse.py:27`, `src/pitwall/observability/langfuse.py:201`).
- Missing optional SDK or client initialization failure: `log.warning("langfuse_client_init_failed", ...)` — no exception propagates (`src/pitwall/observability/langfuse.py:30`, `src/pitwall/observability/langfuse.py:40`).
- `lf.trace()` / `lf.start_span()` failure: `log.warning("langfuse_trace_start_failed", ...)` — returns an `InferenceTrace` with no underlying Langfuse trace (`src/pitwall/observability/langfuse.py:223`, `src/pitwall/observability/langfuse.py:226`, `src/pitwall/observability/langfuse.py:232`, `src/pitwall/observability/langfuse.py:234`, `src/pitwall/observability/langfuse.py:240`).
- `trace.event()` failure: `log.warning("langfuse_event_failed", ...)` — no exception (`src/pitwall/observability/langfuse.py:129`, `src/pitwall/observability/langfuse.py:137`).
- `trace.finish()` failure: `log.warning("langfuse_trace_finish_failed", ...)` — no exception (`src/pitwall/observability/langfuse.py:139`, `src/pitwall/observability/langfuse.py:185`).
- `_safe_trace_value()` truncates strings to 500 chars to avoid sending large payloads (`src/pitwall/observability/langfuse.py:54`, `src/pitwall/observability/langfuse.py:58`).

### Cost Exporter

- **Startup:** `SystemExit(1)` if `DATABASE_URL` is not set (`src/pitwall/cost/exporter.py:64-65`).
- **Poll loop:** `_poll_loop` catches all exceptions from `_refresh`, logs `log.exception("refresh failed: %s", exc)`, and continues polling after 60 s. A single poll failure does not crash the process.
- **DB connection:** `asyncpg.create_pool` will raise if `DATABASE_URL` is malformed; this surfaces as an unhandled exception in the lifespan manager (fatal).
- **Prometheus scrape:** `/metrics` is a synchronous FastAPI function; if `generate_latest()` throws (unusual), the scrape returns a 500.

---

## 7. Testing

| Test file | What it covers |
|---|---|
| `tests/observability/test_scorecards.py` | Hermetic unit tests for `ScorecardBuilder` — determinism, normalisation, ranking, weighting, aggregators (mean/median/p95/sum), geometric vs arithmetic composite, edge cases, validation |
| `tests/property/test_scorecards_properties.py` | Hypothesis property tests — determinism, rank uniqueness/contiguity, normalised bounds [0,1], composite bounds, geometric zero-penalty, single-entity all-ones, empty input, non-increasing ranking |
| `tests/cost/test_cloud_cost_exporter.py` | Parses `config/prometheus/pitwall-cloud-alerts.yml` YAML and asserts alert names/severity; asserts metric names appear in `exporter.py` source; asserts `active_workers` Gauge has `provider` label; asserts no Tailscale JOIN in exporter query |
| `tests/test_full_cost_path.py` | Full hermetic E2E cost path — estimate → budget gate → RunPod billing reconcile → daily rollup → exporter refresh → threshold alert notification — using fakes for DB pool, Redis, RunPod billing. `_refresh` is exercised via the full path (imported transitively) |
| `tests/integration/test_cost_daily_rollup.py` | Daily cost aggregation integration tests |
| `tests/reconciler/test_cost_daily_rollup.py` | Reconciler-side daily rollup logic |
| `tests/test_actual_cost_reconcile.py` | Actual-vs-estimate cost reconciliation |
| `tests/db/test_workload_cost_columns.py` | Workload cost column schema |
| `tests/db/test_cost_daily_migration.py` | Cost-daily migration schema |
| `tests/test_pipeline_cost_exclusion.py` | Cost exclusion logic in pipelines |
| `tests/mcp/test_cost_tools.py` | MCP cost tool wrappers |
| `tests/fixtures/mcp/cost_summary_month.json` | Fixture data for MCP cost tools |

`tests/cost/test_cloud_cost_exporter.py` is the primary direct unit test for the exporter module itself.

---

## 8. Dependencies

### Internal imports

| Module | What is used |
|---|---|
| `pitwall.config` | `get_settings()` (Langfuse settings lookup), `require_runtime_env()` (cost-exporter startup guard) |
| `pitwall.cost.budget_gate` | `BudgetGate` used in `test_full_cost_path.py` (not a runtime import of the exporter) |
| `pitwall.reconciler` | `aggregate_daily_cost`, `apply_terminal_state`, `fetch_active_workloads`, `map_runpod_status` (exercised in full cost path test) |
| `pitwall.core.models` | `Workload` shape consumed by `observations_from_workloads` (duck-typed; no runtime dependency) |

### External libraries

| Library | Used by | Purpose |
|---|---|---|
| `langfuse` | `observability/langfuse.py` | Optional trace emission client, installed by the `tracing` extra and imported lazily by `_get_client()` (`pyproject.toml:61`, `pyproject.toml:62`, `src/pitwall/observability/langfuse.py:30`) |
| `prometheus_client` | `cost/exporter.py` | `Gauge`, `generate_latest`, `CONTENT_TYPE_LATEST` |
| `asyncpg` | `cost/exporter.py` | Postgres async connection pool |
| `fastapi` | `cost/exporter.py` | `FastAPI` app, `Response` |
| `starlette` | `cost/exporter.py` | `Response` class |
| `uvicorn` | `cost_exporter/__main__.py`, `cost/exporter.py` | ASGI server |
| `pydantic` | `config.py` | `BaseModel`, `Field`, `ConfigDict` |
| `asyncpg` | `cost/exporter.py` | Pool creation in lifespan |
