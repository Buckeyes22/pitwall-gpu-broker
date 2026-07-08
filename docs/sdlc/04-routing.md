# Capability Routing & Provider Resolution

## 1. Purpose & Scope

Maps an inference request to an ordered RunPod provider chain. Core layers:

- **Pure planner** (`src/pitwall/routing/`): stateless `plan_route` → `RoutePlan`, no I/O.
- **Price×latency arbitrage** (`src/pitwall/routing/arbitrage.py`): pure weighted
  selector for already-eligible provider/GPU options.
- **Quality-aware routing** (`src/pitwall/routing/quality_routing.py`): pure
  selector that chooses the highest scorecard quality within caller cost/latency
  caps, or a weighted quality-cost-latency blend.
- **Price×latency×carbon arbitrage** (`src/pitwall/routing/arbitrage.py`): pure
  weighted selector for already-eligible provider/GPU options.
- **Carbon-aware scheduling model** (`src/pitwall/routing/carbon.py`): pure
  provider/region carbon-intensity lookup plus weighted objective primitives.
- **Demand-forecast prewarm** (`src/pitwall/routing/prewarm.py`): stateless recent-count forecast → warm-target recommendations, no provisioning I/O.
- **Spot→on-demand failover** (`src/pitwall/routing/failover.py`): provider-status
  inspection plus caller-supplied checkpoint/resume hooks and arbitrage-ranked
  on-demand target provisioning.
- **Runtime resolver** (`src/pitwall/resolver/`): async Stage 1+2 resolution + provider URL construction.

Entry points: `plan_route` (pure), `select_arbitrage_option`/`sort_arbitrage_options`
(pure), `forecast_demand`/`plan_prewarm` (pure), `execute_spot_failover`
(async orchestration), and `resolve_capability` (async, repo-backed).
(pure), `select_quality_routing_option`/`sort_quality_routing_options` (pure),
`forecast_demand`/`plan_prewarm` (pure), and `resolve_capability` (async,
repo-backed).
(pure), `carbon_intensity_for_provider`/`score_carbon_objective` (pure),
`forecast_demand`/`plan_prewarm` (pure), `execute_spot_failover` (async
orchestration), and `resolve_capability` (async, repo-backed).

---

## 2. Components

### `routing/types.py` — All routing DTOs

Frozen/slotted dataclasses. Key types:

| Type | Fields |
|---|---|
| `RoutingRequest` | `capability_name`, `payload_bytes`, `required_gpu_class`, `required_region`, `required_volume_id`, `hints: Hints`, `capability_id`, `required_cuda_min`, `required_cuda_version`, `stream: bool` |
| `Hints` | `latency_sensitive`, `cost_sensitive`, `region_preference` |
| `ConstraintResult` | `provider_id`, `passed`, `reason`, `reasons` |
| `RouteCandidate` | `provider_id`, `provider`, `rank`, `score`, `score_explanation`, `fallback_for`, `explicit_fallback_chain` |
| `RouteAttempt` | `provider_id`, `provider`, `attempt`, `score`, `score_explanation`, `backoff_before_attempt_s` |
| `RoutePlan` | `request`, `attempts`, `ranked_candidates`, `eliminated`, `capacity_decisions`, `max_attempts=3` |
| `CapacityDecision` | `provider_id`, `available: bool|None`, `reason`, `keys`, `selected_key`, `stage=4` |

`EliminationReason` / `ProviderEliminated` enums: `CAPABILITY_MISMATCH`, `REGION_MISMATCH`, `CUDA_MISMATCH`, `GPU_CLASS_MISMATCH`, `PAYLOAD_TOO_LARGE`, `DISABLED`, `HEALTH_UNHEALTHY`, `HEALTH_COOLDOWN`, `CAPACITY_UNAVAILABLE`.

`parse_stream_from_bytes(body: bytes) -> bool`: inspects JSON for `stream=true`.

**Invariants**: `RoutePlan.to_dict()` is order-stable and JSON-serializable. Every input provider appears in exactly one of `ranked_candidates` or `eliminated`.

---

### `routing/constraints.py` — Stage 1 hard constraints (pure)

```python
DEFAULT_LB_MAX_PAYLOAD_MB = Decimal("30")  # serverless_lb default

def filter_hard_constraints(request, providers, *, capability=None, capability_id=None) -> HardConstraintFilterResult
def evaluate_hard_constraints(request, provider, *, capability=None, capability_id=None) -> ConstraintResult
# Aliases: apply_hard_constraints, check_hard_constraints, stage1_hard_constraint_filter
```

**Five constraints in order:**
1. **Capability mismatch** — `capability_name`/`capability_id` must match provider's config (`capability_name`, `capability`, `capability_id`).
2. **Region mismatch** — `required_region` vs provider `region`; `required_volume_id` vs `volume_id`/`networkVolumeId`/`required_volume`.
3. **CUDA mismatch** — if `required_cuda_min`/`required_cuda_version` set, provider must declare compatible version in `allowed_cuda_versions`/`allowedCudaVersions`/`cuda_min`/`cuda_version`/`cuda`. Version compared as numeric tuple `(12, 4)`; string equality fallback.
4. **GPU class mismatch** — if `required_gpu_class` set, provider declares candidates via `gpu_class`/`gpu_type`/`gpu_type_id` fields or `gpu_classes`/`gpu_types`/`gpuTypeIds`/`gpu_type_priority` config lists. Token normalization strips `NVIDIA GEFORCE GENERATION`; suffix match (≥4 chars) accepted.
5. **Payload too large** — if `payload_bytes` known and provider has `max_payload_mb` or is `serverless_lb` (default 30 MB), payload must not exceed limit.

**Invariant**: all pure functions; `evaluate_hard_constraints` returns `passed=True` only when zero constraints fire.

---

### `routing/cooldown.py` — Provider health cooldown state machine (pure)

```python
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_INITIAL_COOLDOWN = timedelta(minutes=5)
DEFAULT_ESCALATED_COOLDOWN = timedelta(minutes=15)
DEFAULT_COOLDOWN_POLICY = CooldownPolicy()

@dataclass(frozen=True)
class ProviderCooldownState:
    consecutive_failures: int = 0
    cooldown_trips: int = 0
    cooldown_until: datetime | None = None
    health_status: str = "unknown"

class CooldownStateMachine:
    def record_success(state, *, now=None) -> ProviderCooldownState
    def record_failure(state, *, now=None) -> ProviderCooldownState
    def apply_probe_result(state, *, passed, now=None) -> ProviderCooldownState

def state_from_provider(provider) -> ProviderCooldownState
def is_in_cooldown(state, *, now=None) -> bool
def cooldown_duration_for_trip(cooldown_trip, *, ...) -> timedelta
def to_provider_patch(state) -> dict[str, object]
# Aliases: record_success, record_failure, next_cooldown_state, is_provider_in_cooldown
```

**Transitions**: On success → `consecutive_failures=0, cooldown_trips=0, cooldown_until=None, health_status="healthy"`. On failure → increment `consecutive_failures`; every `failure_threshold` (3) trips triggers a new cooldown. Trip 1 → 5 min; trips ≥ 2 → 15 min. If already in cooldown window, state returned unchanged.

**Invariants**: `cooldown_trip >= 1`; `initial_cooldown > 0`; `escalated_cooldown >= initial_cooldown`; all datetimes timezone-aware UTC.

---

### `routing/scoring.py` — Stage 3 scoring

```python
def score_provider(provider, hints: Hints | None = None, observed: ObservedMetrics | None = None) -> float
def explain_score(provider, hints: Hints | None = None, observed: ObservedMetrics | None = None) -> ScoreExplanation
```

**Formula** (`explain_score:44-86`):
```
base_score         = 100.0
latency_penalty    = cold_start_p50_ms / 100         (latency_sensitive only)
warm_worker_bonus  = 20.0                            (latency_sensitive AND warm_workers >= 1)
cost_penalty       = cost_per_second_active * 10_000  (cost_sensitive only)
region_bonus       = 15.0                            (region_preference == provider.region)
error_penalty      = recent_error_rate * 50

score_before_mult  = base - latency + warm - cost + region - error
final_score        = score_before_mult * priority_multiplier
```
Fields from provider attributes or `config["…"]`. `recent_error_rate` ≤ 1; all numerics finite non-negative.

---

### `routing/planner.py` — Pure four-stage planner

```python
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_S = 1.0

def plan_route(request, providers=None, *, capability=None, capability_id=None,
               hints=None, observed=None, observed_metrics=None,
               context=None, now=None, max_attempts=3, backoff_base_s=1.0,
               availability_cache=None, capacity_cache=None) -> RoutePlan
# Aliases: build_route_plan, create_route_plan, route_providers
```

**Pipeline**:
1. **S1** `filter_hard_constraints` — capability/region/CUDA/GPU class/payload.
2. **S2** `_stage2_elimination_reasons` — `enabled=False`, `health_status!="healthy"`, `is_in_cooldown`.
3. **S3** `explain_score` per provider; sort `(-score, priority, name, id)`.
4. **S4** `_apply_stage4_capacity` — for `pod_lease` only; queries the `PlanningContext` availability snapshot, or a legacy cache argument, with `is_available(datacenter, gpu_name, cloud_type, gpu_count)`. Provider kept if any key `True`; otherwise eliminated with `CAPACITY_UNAVAILABLE`.
5. **Chain** `_route_attempts` — primary = first non-fallback; fallbacks from `explicit_fallback_chain` or `fallback_for`; capped at `max_attempts`. Backoff: `0s` attempt 1; `backoff_base_s * 2^(n-2)` for attempt n>1.

**Pod-lease capacity key sources**: `datacenter` from `dataCenterIds`/`data_center_id`/`datacenter`/`region`; `cloud_type` normalized to `SECURE`/`ALL` (if `ALL` with volume → `SECURE`); `gpu_count` from field/config default 1; `gpu_names` from `required_gpu_class` or `gpu_name`/`gpu_type`/`gpu_class` fields or `gpu_names`/`gpu_classes`/`gpu_type_priority` config lists.

**Validation** (`ValueError`): `max_attempts < 1`; non-finite/negative `backoff_base_s`; non-UTC `now`; mismatched `availability_cache` vs `capacity_cache`; provider missing `id`.

---

### `routing/context.py` — Replay substrate / `PlanningContext`

`PlanningContext` is the deterministic replay seam for planner and resolver calls. It carries:

- `now: datetime` — always timezone-aware UTC.
- `providers` — immutable provider snapshots, detached from later source-object mutation.
- `capability` — copied capability metadata used by Stage 1 constraints when not supplied directly.
- `availability_snapshot` / `capacity_snapshot` — immutable Stage 4 RunPod availability values.

**Live constructor**:

```python
context = PlanningContext.live()
plan = plan_route(request, providers, capability=capability, context=context)
```

`PlanningContext.live()` captures wall-clock UTC `now` and snapshots the current process-global RunPod availability cache. When callers omit `context`, `plan_route` and `resolve_capability` construct the same live context internally, so existing production behavior is preserved.

**Replay constructor**:

```python
context = PlanningContext.replay(
    now=historical_utc_datetime,
    providers=historical_providers,
    capability=historical_capability,
    availability_entries=[
        ("US-KS-2", "NVIDIA L4", "SECURE", 1, False),
    ],
)
plan = plan_route(request, context=context)
```

Replay callers may pass `availability_snapshot=AvailabilitySnapshot(...)` instead of tuple entries. The same `PlanningContext` must produce byte-identical `RoutePlan.to_dict()` output across repeated calls, independent of wall-clock time, later provider mutations, or later global availability-cache changes.

**Discovery integration:** `pitwall.runpod_client.discovery.GpuDiscoveryService` calls `gpu_types()` and `datacenters()` through the GraphQL client, normalizes the response into `GpuDiscoverySnapshot`, and can flatten it into `AvailabilitySnapshot` via `snapshot.to_availability_snapshot()`.  This lets a background refresh task keep the global availability cache warm, while replay callers freeze a historical snapshot and pass it to `PlanningContext.replay(...)` without touching live GraphQL.

**Contract for E11 downstream work**: the What-If Cost Simulator (552), Autopilot (576), and counterfactual time-machine (578) should call `PlanningContext.replay(...)` with explicit historical or hypothetical provider/capacity data, then call `plan_route(..., context=context)`. `context` is mutually exclusive with legacy `now`, `availability_cache`, and `capacity_cache` arguments so there is one authoritative world snapshot per call.

---

### `routing/coalescing.py` — In-flight request coalescing

```python
class AsyncRequestCoalescer[T]:
    async def run(self, key: str, execute: Callable[[], Awaitable[T]]) -> T

def build_inference_coalescing_key(*, idempotency_key, capability_id,
                                   provider_id, capability_params) -> str
```

`AsyncRequestCoalescer` collapses concurrent duplicate work in one API process.
The first caller for a key owns the execution; concurrent callers await the same
future and receive the same result. If the owner raises, the same exception is
observed by all waiters. The key is evicted on success, failure, or cancellation,
so only in-flight requests coalesce.

`POST /v1/inference` uses this after pre-spend guardrails, completed
idempotency replay, provider resolution, and dry-run handling. The coalesced
execution covers the sync budget/RunPod call and trace/header assembly, so a
burst of identical requests produces one upstream execution and one trace emit.

**Keying contract**:

- Key content includes the resolved `capability_id`, selected `provider_id`, and
  canonical redacted `capability_params`.
- Requests with an `Idempotency-Key` use a scope of
  `hash(idempotency_key) + hash(content)`. Different idempotency keys never
  share work, even when payloads match.
- Anonymous requests use `hash(content)`.
- Raw idempotency keys are not stored in coalescing keys.

This is intentionally process-local. Multi-process or multi-host request
coalescing remains the responsibility of durable idempotency and database
constraints.

---

### `routing/arbitrage.py` — Price×latency×carbon arbitrage

```python
@dataclass(frozen=True) class ArbitrageOption:
    provider_id: str
    gpu: str
    price: Decimal
    latency_ms: Decimal
    region: str | None = None

@dataclass(frozen=True) class ArbitrageScore:
    option: ArbitrageOption
    lambda_weight: Decimal
    objective: Decimal
    cost_component: Decimal
    latency_component: Decimal
    carbon_weight: Decimal = Decimal("0")
    carbon_intensity_gco2_per_kwh: Decimal = Decimal("0")
    carbon_component: Decimal = Decimal("0")
    cost_weight: Decimal = Decimal("1")

def score_arbitrage_option(option, *, lambda_weight, carbon_weight=Decimal("0"),
                           carbon_source=None, cost_weight=Decimal("1")) -> ArbitrageScore
def sort_arbitrage_options(options, *, lambda_weight, carbon_weight=Decimal("0"),
                           carbon_source=None, cost_weight=Decimal("1")) -> tuple[ArbitrageScore, ...]
def select_arbitrage_option(options, *, lambda_weight, carbon_weight=Decimal("0"),
                            carbon_source=None, cost_weight=Decimal("1")) -> ArbitrageScore
```

The arbitrage selector ranks already-eligible provider/GPU options by:

```text
objective =
  cost_weight * price
  + lambda_weight * latency_ms
  + carbon_weight * carbon_intensity_gco2_per_kwh
```

`price`, `latency_ms`, `lambda_weight`, `cost_weight`, `carbon_weight`, and
carbon intensities are finite, non-negative `Decimal` values. The selector is
pure and deterministic. Ties sort by objective, weighted cost, latency, weighted
carbon, provider id, then GPU name; `select_arbitrage_option` returns the first
ranked score. `lambda_weight=0` and `carbon_weight=0` preserve cheapest-option
selection. Very large `lambda_weight` values make latency dominate; positive
`carbon_weight` lets lower-carbon providers/regions win when cost and latency
are comparable.

This module does not fetch prices, estimate latency, apply hard constraints,
probe capacity, hedge requests, or build provider URLs. Callers feed it the
pricing union output plus per-provider latency estimates after the existing
eligibility pipeline has produced candidate options.

### `routing/quality_routing.py` — Quality-aware routing

```python
@dataclass(frozen=True) class QualityRoutingOption:
    provider_id: str
    model_id: str
    quality: Decimal
    cost_usd: Decimal
    latency_ms: Decimal

@dataclass(frozen=True) class QualityRoutingPolicy:
    max_cost_usd: Decimal | None = None
    max_latency_ms: Decimal | None = None
    quality_weight: Decimal = Decimal("1")
    cost_weight: Decimal = Decimal("0")
    latency_weight: Decimal = Decimal("0")

@dataclass(frozen=True) class QualityRoutingScore:
    option: QualityRoutingOption
    policy: QualityRoutingPolicy
    objective: Decimal
    quality_component: Decimal
    cost_penalty: Decimal
    latency_penalty: Decimal

def quality_option_from_scorecard(scorecard, *, model_id,
                                  use_normalized_quality=True) -> QualityRoutingOption
def score_quality_routing_option(option, *, policy=None) -> QualityRoutingScore
def sort_quality_routing_options(options, *, policy=None) -> tuple[QualityRoutingScore, ...]
def select_quality_routing_option(options, *, policy=None) -> QualityRoutingScore
```

Default policy ranks already-eligible provider/model candidates by highest
scorecard quality. `max_cost_usd` and `max_latency_ms` are hard caps applied
before selection; if either cap removes all candidates, selection raises
`ValueError`. Weighted mode ranks by:

```text
objective = quality_weight * quality
          - cost_weight * cost_usd
          - latency_weight * latency_ms
```

All numeric inputs are finite, non-negative `Decimal` values, and `quality` is
bounded to `[0, 1]`. At least one weight must be positive. Ties sort by
objective descending, quality descending, cost ascending, latency ascending,
provider id, then model id. `quality_option_from_scorecard` consumes the 573
`EntityScorecard` signal by copying cost/latency and using `quality_normalized`
by default.

This module does not fetch scorecards, apply hard constraints, probe capacity,
hedge requests, or build provider URLs. Callers feed it post-eligibility
candidates with scorecard-derived quality/cost/latency values.
eligibility pipeline has produced candidate options. If `carbon_weight > 0` and
no `carbon_source` is supplied, the module uses
`DEFAULT_CARBON_INTENSITY_SOURCE`.

### `routing/carbon.py` — Carbon-aware scheduling model

```python
class CarbonIntensitySource(Protocol):
    def intensity_for(self, *, provider_id: str, region: str | None) -> Decimal: ...

@dataclass(frozen=True) class StaticCarbonIntensitySource:
    provider_region_intensities: Mapping[tuple[str, str], Decimal] = {}
    region_intensities: Mapping[str, Decimal] = DEFAULT_REGION_CARBON_INTENSITIES_GCO2_PER_KWH
    default_intensity: Decimal = DEFAULT_UNKNOWN_CARBON_INTENSITY_GCO2_PER_KWH

@dataclass(frozen=True) class CarbonObjectiveWeights:
    cost_weight: Decimal = Decimal("1")
    latency_weight: Decimal = Decimal("0")
    carbon_weight: Decimal = Decimal("0")

def carbon_intensity_for_provider(provider, *, source=None) -> Decimal
def score_carbon_objective(*, cost, latency_ms,
                           carbon_intensity_gco2_per_kwh,
                           weights) -> CarbonObjectiveScore
```

`StaticCarbonIntensitySource` is the default deterministic data source. It first
checks an exact `(provider_id, region)` override, then a region/default
datacenter value, then `DEFAULT_UNKNOWN_CARBON_INTENSITY_GCO2_PER_KWH`. The
default table is a model input, not a live data feed; callers can inject any
`CarbonIntensitySource` implementation for live grid data or policy-specific
tables.

`carbon_intensity_for_provider` accepts provider-shaped mappings and model
objects. Region lookup checks direct provider fields (`region`, `datacenter`,
`dataCenterId`, `data_center_id`, `datacenter_id`, `dc_id`), then config fields
including `dataCenterIds`/`data_center_ids`. This lines up with RunPod
datacenter ids from `GpuDiscoverySnapshot` and Stage 4 capacity-key extraction.

**Invariants**:
- All scoring and lookups are pure, deterministic, and network-free.
- All objective inputs and weights are finite, non-negative `Decimal` values.
- `carbon_weight=0` makes carbon a reported component only; it does not change
  ranking.
- Unknown regions receive a non-zero fallback intensity so missing metadata does
  not get a free low-carbon advantage.

### `routing/semantic_cache.py` - Budget-aware semantic cache

```python
class BudgetAwareSemanticCache[T]:
    async def run(*, capability_id, provider_id, capability_params,
                  estimated_cost_usd, execute) -> SemanticCacheRunResult[T]

class SemanticCachePolicy:
    def should_cache(estimated_cost_usd) -> bool
    def ttl_for(estimated_cost_usd) -> timedelta

def build_semantic_cache_key(*, capability_id, provider_id,
                             capability_params, hasher=None) -> str
```

`BudgetAwareSemanticCache` is a process-local result cache for completed
inference work. It is keyed by a semantic signature, not an idempotency key:
the default `CanonicalSemanticHasher` collapses whitespace in strings, sorts
mapping keys, preserves sequence order, and hashes the canonical payload. The
cache key also includes a hashed namespace for the resolved `capability_id` and
`provider_id`, so results do not cross provider/model boundaries by default.
Callers can supply a `SemanticSignatureHasher` when an embedding-aware or
domain-specific signature is available.

**Budget behavior**:

- Cache hits return before the caller's `execute` callback runs. The callback is
  where the sync budget gate and provider I/O should live, so hits avoid a new
  admission/spend path.
- `SemanticCachePolicy.min_cache_estimate_usd` skips retention for cheap
  requests where the memory churn is not worth the avoided spend.
- `SemanticCachePolicy.high_value_estimate_usd` extends TTL with
  `high_value_ttl_multiplier`, keeping expensive results hot longer.
- Size eviction first purges expired entries, then removes the lowest estimated
  cost entries, with least-recently accessed entries losing ties. This biases
  bounded cache space toward higher-spend avoided work.

**Invariants**:

- The cache never stores exceptions.
- Raw prompt text is never embedded in cache keys; signatures are hashed before
  key assembly.
- TTLs require timezone-aware UTC clocks; tests use an injected clock for
  deterministic expiry.
- This is not durable idempotency and does not replace database workload
  records. It is a routing-side optimization for repeat semantic content within
  one process.

---

### `finops/bidding.py` — Real-time cross-provider spot bidding

```python
@dataclass(frozen=True) class SpotPrice:
    provider_id: str
    resource_id: str
    gpu: str
    minimum_bid_usd_per_hour: Decimal
    current_bid_usd_per_hour: Decimal | None = None
    gpu_count: int = 1
    available: bool = True

@dataclass(frozen=True) class BiddingPolicy:
    target_price_usd_per_hour: Decimal
    max_price_usd_per_hour: Decimal
    target_capacity_units: int = 1
    max_parallel_bids: int = 1
    bid_increment_usd_per_hour: Decimal = Decimal("0")
    min_adjustment_usd_per_hour: Decimal = Decimal("0.000001")
    max_total_bid_usd_per_hour: Decimal | None = None
    allow_bid_decrease: bool = False
    dry_run: bool = True
    policy_evaluation: PolicyEvaluationResult | None = None
    budget_decision: CircuitBreakerDecision | None = None

class BiddingEngine:
    def evaluate(snapshot: SpotPriceSnapshot, policy: BiddingPolicy) -> BiddingPlan

async def collect_spot_price_snapshot(feeds, *, observed_at) -> SpotPriceSnapshot
async def execute_bidding_plan(plan, placer, *, apply=False) -> tuple[BidPlacementReceipt, ...]
```

The bidding engine consumes already-normalized live spot lanes, such as RunPod
GraphQL minimum bids and Vast bid prices. It does not call provider APIs while
planning. Given the same `SpotPriceSnapshot` and `BiddingPolicy`, it emits the
same ordered `BiddingPlan`.

**Decision rule**:

```text
desired_bid = target_price                     when minimum_bid <= target_price
desired_bid = minimum_bid + bid_increment       when target_price < minimum_bid <= max_price
block                                             when desired_bid > effective_max_price
```

`effective_max_price` is `max_price_usd_per_hour` unless the budget circuit
breaker action is `downgrade`, in which case the evaluator caps max at the
target price. A circuit-breaker `block` decision or denied
`PolicyEvaluationResult` blocks all bid placements. `max_total_bid_usd_per_hour`
caps selected bid value across selected lanes.

**Action selection**:

- `place` when the selected lane has no current bid.
- `raise` when current bid is below desired bid by more than the adjustment band.
- `lower` when current bid is above desired bid and `allow_bid_decrease=True`.
- `hold` when the selected lane is already inside the adjustment band, or when
  an otherwise eligible lane is not selected.
- `block` when capacity is unavailable, the desired bid exceeds the effective
  max, policy denies, or budget rails deny.

Selection is deterministic: eligible lanes sort by minimum bid, provider id,
resource id, then GPU name. The default policy selects one lane and leaves every
other eligible lane as a recommendation-only hold.

**Execution gate**: `execute_bidding_plan(..., apply=False)` is a no-op. Actual
provider adapters are called only for selected executable actions and only when
the caller explicitly passes `apply=True`. The module-level placer protocol is
generic enough to wrap `RunpodGraphQLClient.set_bid_price(...)` for RunPod and a
Vast adapter that writes the selected hourly bid price into the provider-specific
bid payload. Secrets remain in adapter credentials and are never interpolated
into URLs.

**Invariant**: Pure plan evaluation, explicit UTC snapshot time, `Decimal`
prices quantized to 0.000001 USD, stable `to_dict()` output, no network I/O, no
provider mutation unless the separate apply gate is opened.

---

### `routing/prewarm.py` — Demand-forecast prewarm

```python
@dataclass(frozen=True) class DemandSample: capability_id, observed_at, request_count
@dataclass(frozen=True) class PrewarmPolicy: lookback, sample_window, forecast_window, forecast_horizon, headroom, default_requests_per_warm_unit, min_forecast_requests, max_targets_per_capability, default_lead_time, recommendation_ttl
@dataclass(frozen=True) class DemandForecast: capability_id, window_start, window_end, observed_counts, projected_requests, source_window_start, source_window_end
@dataclass(frozen=True) class PrewarmRecommendation: capability_id, provider_id, provider_type, target_kind, target_count, current_warm_count, requests_per_warm_unit, forecast_requests, start_at, ready_by, expires_at, reason, target, rank
@dataclass(frozen=True) class PrewarmPlan: now, forecasts, recommendations

def forecast_demand(history, *, now, policy=None) -> tuple[DemandForecast, ...]
def plan_prewarm(history, providers, *, now, policy=None) -> PrewarmPlan
```

`forecast_demand` buckets recent `DemandSample` counts into fixed UTC windows ending at `now`. For each capability with recent data, it projects the near-term window as:

```text
projected = ceil((latest_window_count + max(0, positive_trend_from_oldest_to_latest)) * headroom)
```

The forecast window starts at `now + forecast_horizon` and lasts `forecast_window`. Output ordering is by `capability_id`; `to_dict()` serializes datetimes as UTC ISO strings.

`plan_prewarm` converts forecasts into recommendation-only warm targets. It filters providers to enabled, not unhealthy, not in cooldown, and one of `serverless_lb`, `serverless_queue`, or `pod_lease`; then sorts by `(priority, name, id)` and caps per capability with `max_targets_per_capability`.

**Warm target kinds**:

| Provider type | `target_kind` | Current warm count source | Target payload |
|---|---|---|---|
| `serverless_lb`, `serverless_queue` | `endpoint_workers` | `config["workers"]["workers_min"]`, `workers_min`, or `warm_workers` | `runpod_endpoint_id` only; no URL construction |
| `pod_lease` | `pod_lease` | `warm_pods` | `runpod_template_id`, datacenter, GPU name/count, cloud type |

**Sizing**: `target_count = ceil(projected_requests / requests_per_warm_unit)`. Provider config may override unit capacity through `config["prewarm"]` (`requests_per_warm_worker`, `requests_per_warm_pod`, or `requests_per_warm_unit`). Recommendations are emitted only when `target_count > current_warm_count`.

**Timing**: `ready_by = forecast.window_start`; `start_at = max(now, ready_by - max(default_lead_time, provider cold-start/prewarm lead time))`; `expires_at = forecast.window_end + recommendation_ttl`.

**Invariant**: Pure and deterministic for identical history, provider snapshots, `now`, and policy. It never creates pods, updates `workersMin`, calls RunPod, or emits RunPod URLs.

---

### `routing/failover.py` — Spot→on-demand failover

```python
class FailoverCapacityMarket(StrEnum):
    SPOT = "spot"
    PREEMPTIBLE = "preemptible"
    ON_DEMAND = "on_demand"

@dataclass(frozen=True) class FailoverSource:
    provider_plugin_id, provider_record, credentials, external_id, lease_id, market

@dataclass(frozen=True) class FailoverCheckpoint:
    token: str
    state: Mapping[str, Any]
    captured_at: datetime | None

@dataclass(frozen=True) class FailoverTarget:
    provider_plugin_id, provider_record, credentials, gpu, price, latency_ms,
    market, provision_payload, extra_env

@dataclass(frozen=True) class FailoverTargetSelection:
    target: FailoverTarget
    score: ArbitrageScore
    rank: int

@dataclass(frozen=True) class FailoverRequest:
    context, registry, capability, source, targets, checkpoint, resume,
    lambda_weight, request_id, budget_gate, idempotency_key, dry_run

async def execute_spot_failover(request: FailoverRequest[T]) -> FailoverResult[T]
def is_preempted_status(status: StatusResult) -> bool
def select_on_demand_failover_target(targets, *, lambda_weight) -> FailoverTargetSelection
def sort_on_demand_failover_targets(targets, *, lambda_weight) -> tuple[FailoverTargetSelection, ...]
```

**Flow**:
1. Look up `source.provider_plugin_id` in `ProviderRegistry`, validate credentials, and call provider `status(...)`.
2. Detect preemption from `raw["pitwall_preempted"] == true`, or from provider-neutral `ResourceStatus.FAILED` plus status text markers (`preempted`, `outbid`, `interrupted`, `evicted`, `preempted_by_bid`).
3. If the source is not preempted, return `FailoverResult(preempted=False, resumed=False)` without checkpointing or provisioning.
4. Call the application checkpoint hook with `FailoverCheckpointRequest(context, source, status)`. The controller does not invent checkpoint formats; callers own durable state capture and return `FailoverCheckpoint(token, state, captured_at)`.
5. Filter `FailoverTarget` entries to `market == ON_DEMAND`, then rank them with the existing arbitrage formula (`price + lambda_weight * latency_ms`). Ties use objective, price, latency, provider id, then GPU.
6. Provision the selected target through the registry provider with `ProvisionRequest`, passing the target's `provision_payload`, `extra_env`, budget gate, idempotency key, and dry-run flag unchanged.
7. Call the resume hook with `FailoverResumeRequest`, which carries source status, checkpoint, target selection, and provision result. The returned value is stored on `FailoverResult.resume_result`.

**Responsibilities**: This module orchestrates status/checkpoint/provision/resume only. It does not update lease rows directly, build provider URLs, fetch prices, probe capacity, construct checkpoint contents, or suppress hook errors. Checkpoint failure stops before provisioning so the controller does not create orphan on-demand capacity after a failed state capture.

---

### `routing/openai.py` — OpenAI-compatible chain

```python
OPENAI_PROVIDER_TYPES = frozenset({SERVERLESS_QUEUE, SERVERLESS_LB, PUBLIC_ENDPOINT})
DEFAULT_OPENAI_MAX_ATTEMPTS = 3
MAX_OPENAI_ATTEMPTS = 3

def resolve_openai_provider_chain(providers, *, primary_provider_id=None,
                                  max_attempts=3, now=None) -> OpenAIProviderChain
def openai_base_url_for_provider(provider) -> str | None
def build_openai_url(openai_base_url, path) -> str
# Aliases: resolve_openai_chain, resolve_provider_chain, resolve_openai_provider_ids
```

**URL derivation from `runpod_endpoint_id`**: `serverless_lb` → `https://{id}.api.runpod.ai/openai/v1`; `serverless_queue`/`public_endpoint`/`None` → `https://api.runpod.ai/v2/{id}/openai/v1`; `pod_lease` → `None`. `config["openai_base_url"]` overrides if present. `build_openai_url` strips leading `/` and `v1/` from path to prevent `/v1/v1/` duplication.

**Ordering**: filters to `OPENAI_PROVIDER_TYPES`, enabled, healthy, not in cooldown; sorts `(priority, name, id)`; primary first; explicit `fallback_chain` appended; remaining candidates fill slots up to `max_attempts`.

**Validation** (`ValueError`): `max_attempts < 1` or boolean; `primary_provider_id` not in available candidates.

---

### `routing/fallback.py` — Async OpenAI fallback executor

```python
DEFAULT_OPENAI_FALLBACK_BUDGET_S = 5.0

@dataclass(frozen=True)
class OpenAIProxyRequest:
    method, path, headers: Mapping, body: bytes, client: httpx.AsyncClient
    fallback_budget_s: float = 5.0, max_attempts: int = 3

@dataclass(frozen=True)
class OpenAIProxyResult:
    response: httpx.Response, provider: Provider
    attempted_provider_ids: tuple[str, ...], elapsed_s: float

class OpenAIProxyExecutionError(RuntimeError):
    attempted_provider_ids, cause, attempted_errors

async def execute_openai_with_fallback(request_ctx, providers: list[Provider],
                                        *, on_attempt=None) -> OpenAIProxyResult
```

Retries only on 5xx or transport errors before response headers; 4xx returned immediately. Budget enforced per-attempt. `on_attempt` callback called after each attempt with current `attempted_provider_ids`. Raises `OpenAIProxyExecutionError` on exhaustion (includes `attempted_provider_ids`, `attempted_errors`, `cause`). Validation: `max_attempts >= 1`, `fallback_budget_s > 0`.

---

### `routing/hedging.py` — Hedged racing

```python
DEFAULT_HEDGE_DELAY_S = 0.050
DEFAULT_HEDGED_MAX_ATTEMPTS = 2
DEFAULT_HEDGED_MAX_CONCURRENCY = 2

@dataclass(frozen=True)
class HedgedProviderRequest:
    providers: Sequence[ProviderT]
    call_provider: Callable[[ProviderT], Awaitable[ResultT]]
    hedge_delay_s: float = 0.050
    max_attempts: int = 2
    max_concurrency: int = 2
    provider_id: Callable[[ProviderT], str]

@dataclass(frozen=True)
class HedgedProviderResult:
    value: ResultT
    provider: ProviderT
    provider_id: str
    attempted_provider_ids: tuple[str, ...]
    elapsed_s: float

class HedgedProviderError(RuntimeError):
    attempted_provider_ids, cause, attempted_errors

async def race_providers(request: HedgedProviderRequest) -> HedgedProviderResult
```

The hedger accepts an already ordered provider chain from `RoutePlan.attempts`, `OpenAIProviderChain.providers`, or another resolver. It starts the primary immediately, waits `hedge_delay_s`, then starts backup attempts only if no provider has succeeded. The first successful awaitable wins; every other in-flight task is cancelled and drained. If active attempts fail before the hedge delay, the next provider starts immediately so failures do not wait out the latency hedge timer.

**Bounded fan-out**: the executor starts at most `max_attempts` total providers and runs at most `max_concurrency` calls concurrently. Defaults are intentionally two-provider hedging (`primary + one backup`) because hedging spends extra upstream capacity. Callers should enable it only for latency-sensitive requests and should combine it with planner cost hints, per-request budget admission, and provider pricing metadata before increasing either bound.

**Validation** (`ValueError`): non-empty providers, finite non-negative `hedge_delay_s`, positive integer `max_attempts`/`max_concurrency`, non-empty unique provider ids.

---

### `routing/cascade.py` — Cascade routing

```python
@dataclass(frozen=True)
class CascadeTier:
    provider: ProviderT
    estimated_cost_usd: Decimal

@dataclass(frozen=True)
class CascadeGateDecision:
    passed: bool
    confidence: float | None = None
    reason: str | None = None

@dataclass(frozen=True)
class CascadeProviderRequest:
    tiers: Sequence[CascadeTier[ProviderT]]
    call_provider: Callable[[ProviderT], Awaitable[ResultT]]
    quality_gate: Callable[[ProviderT, ResultT],
                           CascadeGateDecision | Awaitable[CascadeGateDecision]]
    max_attempts: int | None = None
    provider_id: Callable[[ProviderT], str]
    cost_of_result: Callable[[ProviderT, ResultT], Decimal | float | int | str] | None

@dataclass(frozen=True)
class CascadeProviderResult:
    value: ResultT
    provider: ProviderT
    provider_id: str
    attempts: tuple[CascadeAttempt[ProviderT, ResultT], ...]
    total_cost_usd: Decimal

class CascadeRoutingError(RuntimeError):
    attempts, attempted_provider_ids, total_cost_usd

async def route_with_cascade(request: CascadeProviderRequest) -> CascadeProviderResult
```

The cascade executor accepts an already ordered cheap-to-expensive tier chain.
It calls one provider at a time, passes the output through a caller-supplied
quality/confidence gate, and stops at the first passing gate. A gate failure is
not treated as a provider failure: the output was produced, the attempt cost is
counted, and the next stronger tier is tried. This keeps complexity escalation
separate from transport fallback and latency hedging.

Each `CascadeAttempt` records `provider_id`, attempt number, output value, gate
decision, and `cost_usd`. `total_cost_usd` is the sum of all attempted tiers.
By default attempt cost is the tier's `estimated_cost_usd`; callers that can
derive actual token/request cost from the model output may pass `cost_of_result`
to override the estimate per attempt.

**Validation** (`ValueError`): non-empty tiers, positive integer `max_attempts`,
non-empty unique provider ids, non-decreasing `estimated_cost_usd`, finite
non-negative USD costs, gate confidence between 0 and 1, and quality gates that
return `CascadeGateDecision`.

---

### `routing/canary.py` — Shadow/canary routing

```python
@dataclass(frozen=True) class CanaryRoutingPolicy: mode, candidate_fraction, experiment_id
@dataclass(frozen=True) class CanaryProviderRequest: baseline_provider, candidate_provider, call_provider, policy, traffic_key, traffic_bucket, observe_result, provider_id
@dataclass(frozen=True) class CanaryRoutingResult: value, served_provider, served_provider_id, observations, baseline_observation, candidate_observation
@dataclass(frozen=True) class CanaryObservation: provider_id, success, latency_ms, cost_usd, quality_score
@dataclass(frozen=True) class ProviderMetrics: request_count, success_count, average_latency_ms, average_cost_usd, average_quality_score
@dataclass(frozen=True) class CanaryPromotionPolicy: min samples, promote thresholds, rollback guardrails

def stable_traffic_bucket(traffic_key, *, experiment_id) -> float
def select_canary_traffic(policy, traffic_key, *, traffic_bucket=None) -> CanaryTrafficDecision
async def route_with_canary(request: CanaryProviderRequest) -> CanaryRoutingResult
def evaluate_canary(comparison, *, policy=CanaryPromotionPolicy()) -> CanaryPromotionDecision
```

`stable_traffic_bucket` hashes `experiment_id + traffic_key` with SHA-256 and
maps it into `[0.0, 1.0)`. The candidate is selected when
`bucket < candidate_fraction`; callers may pass `traffic_bucket` in tests or
replay workflows to make the route decision explicit.

**Shadow mode**: selected traffic calls the baseline and mirrors the candidate,
but the returned `value` and `served_provider_id` always come from the baseline.
Candidate shadow failures are recorded as unsuccessful candidate observations
and do not alter the served result. A fraction of `0.0` does not call the
candidate; `1.0` mirrors every request.

**Canary mode**: selected traffic serves the candidate; unselected traffic serves
the baseline. The controller returns observations for the provider(s) it called.
The module does not write metrics, change provider priority, or mutate GitOps
state; callers persist the returned observations and feed aggregate windows to
`evaluate_canary`.

**Auto-promote decision**: `evaluate_canary` compares `ProviderMetrics` windows
for baseline and candidate. It holds until both sample floors are met, rolls
back when configured guardrails regress (success rate, quality, latency, or
cost), promotes when the candidate passes all promote thresholds and beats the
baseline in at least one comparable dimension, otherwise holds.

**Validation** (`ValueError`): finite `candidate_fraction` in `[0, 1]`, finite
explicit `traffic_bucket` in `[0, 1)`, non-empty unique provider ids,
non-negative metric totals/counts, `quality_score` in `[0, 1]`, and finite
promotion thresholds.

---

### `resolver/service.py` — Async Stage 1+2 resolver

```python
class CapabilityRepositoryLike(Protocol):
    async def get(capability_id) -> Capability | None
    async def get_by_name(name) -> Capability | None

class ProviderRepositoryLike(Protocol):
    async def get(provider_id) -> Provider | None
    async def list(*, capability_id=None, enabled_only=False, provider_type=None,
                   limit=100, offset=0) -> list[Provider]

@dataclass(frozen=True)
class Stage12Resolution:
    capability: Capability, provider: Provider
    eligible_providers: tuple[Provider, ...]
    eliminated: tuple[RouteElimination, ...] = field(default_factory=tuple)

async def resolve_capability(capability_name, *, capability_repo, provider_repo,
                             provider_id=None, request=None, context=None, now=None,
                             provider_limit=100) -> Stage12Resolution
def select_stage12_provider(request, providers, *, capability,
                            context=None, now=None) -> Stage12Resolution
```

`resolve_capability`: look up by name then id → `CapabilityNotFoundError`. If `enabled=False` → `CapabilityDisabledError`. Fetch providers (single by `provider_id` or list from repo). Delegate to `select_stage12_provider`: Stage 1 → Stage 2 → lowest `priority` survivor. Raises `NoHealthyProviderError` if none survive.

---

### `resolver/provider_urls.py` — Provider URL builder with SSRF protection

```python
_ENDPOINT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")

def openai_base_url(provider) -> str
def queue_url(provider, path: str = "") -> str
def lb_url(provider, path: str = "/") -> str
def public_endpoint_url(provider) -> str
def provider_url(provider) -> str
```

| ProviderType | Function | URL pattern |
|---|---|---|
| `serverless_queue` | `queue_url` | `https://api.runpod.ai/v2/{endpoint_id}` |
| `serverless_lb` | `lb_url` | `https://{endpoint_id}.api.runpod.ai{path}` |
| `public_endpoint` | `public_endpoint_url` | `https://api.runpod.ai/v2/{endpoint_id}/openai/v1` |
| `pod_lease` | raises `ValueError` | n/a |

`_require_endpoint_id` validates `runpod_endpoint_id` against allow-list regex. First char must be alphanumeric; remaining 1–63 chars alphanumeric/`-`/`_` only. Blocks host injection, metadata IP (`169.254.169.254`), path traversal (`../..`), userinfo (`@`), port injection. Raises `ValueError` on absent/invalid id.

---

### `resolver/exceptions.py` / `resolver/result.py`

```python
class ResolverError(RuntimeError):
    error_code: str = "resolver_error"
    def to_dict(self) -> dict[str, Any]: ...

class CapabilityNotFoundError(ResolverError): error_code = "capability_not_found"
class CapabilityDisabledError(ResolverError): error_code = "capability_disabled"
class NoHealthyProviderError(ResolverError): error_code = "no_healthy_provider"
class ProviderNotFoundError(ResolverError): error_code = "provider_not_found"
class ProviderExhaustedError(ResolverError): error_code = "provider_chain_exhausted"

@dataclass(frozen=True) class ResolvedProvider: provider: Provider; is_fallback: bool = False
@dataclass(frozen=True) class ResolutionFailure: reason, capability_name, providers_tried: list[str]
ResolutionResult = ResolvedProvider | ResolutionFailure
```

---

## 3. Routing Pipeline

```
RoutingRequest + Iterable[Provider]
  → Stage 1: filter_hard_constraints (cap/region/CUDA/GPU/payload)
  → Stage 2: enabled? health? cooldown? → eligible
  → Stage 3: explain_score → sort (-score, priority, name, id)
  → Stage 4: pod_lease capacity probe via PlanningContext availability snapshot
  → Optional price×latency×carbon arbitrage:
      score eligible provider/GPU options by cost + λ·latency + carbon_weight·carbon
  → _route_attempts: primary + fallbacks (explicit_chain or fallback_for)
  → RoutePlan(attempts, ranked_candidates, eliminated, capacity_decisions)
  → Optional shadow/canary wrapper: route/mirror chosen baseline vs candidate traffic
```

Spot/preemptible lease failover is a separate operational path:

```
FailoverSource + ProviderRegistry
  → provider.status(StatusRequest)
  → is_preempted_status
  → checkpoint(FailoverCheckpointRequest)
  → select ON_DEMAND FailoverTarget via price×latency arbitrage
  → provider.provision(ProvisionRequest)
  → resume(FailoverResumeRequest)
  → FailoverResult
```

Backoff: attempt 1 = 0s; attempt n>1 = `backoff_base_s * 2^(n-2)`.

**Provider URL resolution** (`resolver/provider_urls.py`):
1. `_require_endpoint_id(provider)` — validate against allow-list regex
2. Dispatch by `provider.provider_type` to `lb_url`/`queue_url`/`public_endpoint_url`
3. Interpolate validated id into URL template

`routing/openai.py:openai_base_url_for_provider` checks `config["openai_base_url"]` first; derives from `runpod_endpoint_id` if absent. `build_openai_url` prevents `/v1/v1/` duplication.

---

## 4. Public Interfaces

```python
# Pure planner
def plan_route(request, providers=None, *, capability=None, capability_id=None,
               hints=None, observed=None, observed_metrics=None,
               context=None, now=None, max_attempts=3, backoff_base_s=1.0,
               availability_cache=None, capacity_cache=None) -> RoutePlan

# Async capability resolver
async def resolve_capability(capability_name, *, capability_repo, provider_repo,
                              provider_id=None, request=None, context=None, now=None,
                              provider_limit=100) -> Stage12Resolution
def select_stage12_provider(request, providers, *, capability,
                              context=None, now=None) -> Stage12Resolution

# OpenAI chain
def resolve_openai_provider_chain(providers, *, primary_provider_id=None,
                                   max_attempts=3, now=None) -> OpenAIProviderChain
def openai_base_url_for_provider(provider) -> str | None
def build_openai_url(openai_base_url, path) -> str

# Async fallback executor
async def execute_openai_with_fallback(request_ctx: OpenAIProxyRequest,
                                        providers: list[Provider],
                                        *, on_attempt=None) -> OpenAIProxyResult

# Hedged racing
async def race_providers(request: HedgedProviderRequest) -> HedgedProviderResult

# Cascade routing
async def route_with_cascade(request: CascadeProviderRequest) -> CascadeProviderResult

# Spot/preemptible failover
async def execute_spot_failover(request: FailoverRequest[T]) -> FailoverResult[T]
def is_preempted_status(status: StatusResult) -> bool
def select_on_demand_failover_target(targets: Iterable[FailoverTarget],
                                     *, lambda_weight: Decimal) -> FailoverTargetSelection
def sort_on_demand_failover_targets(targets: Iterable[FailoverTarget],
                                    *, lambda_weight: Decimal) -> tuple[FailoverTargetSelection, ...]
# Shadow/canary routing
async def route_with_canary(request: CanaryProviderRequest) -> CanaryRoutingResult
def evaluate_canary(comparison: CanaryComparison,
                    *, policy: CanaryPromotionPolicy) -> CanaryPromotionDecision
def stable_traffic_bucket(traffic_key: str, *, experiment_id: str) -> float

# Price×latency arbitrage
def score_arbitrage_option(option: ArbitrageOption,
                           *, lambda_weight: Decimal,
                           carbon_weight: Decimal = Decimal("0"),
                           carbon_source: CarbonIntensitySource | None = None,
                           cost_weight: Decimal = Decimal("1")) -> ArbitrageScore
def sort_arbitrage_options(options: Iterable[ArbitrageOption],
                           *, lambda_weight: Decimal,
                           carbon_weight: Decimal = Decimal("0"),
                           carbon_source: CarbonIntensitySource | None = None,
                           cost_weight: Decimal = Decimal("1")) -> tuple[ArbitrageScore, ...]
def select_arbitrage_option(options: Iterable[ArbitrageOption],
                            *, lambda_weight: Decimal,
                            carbon_weight: Decimal = Decimal("0"),
                            carbon_source: CarbonIntensitySource | None = None,
                            cost_weight: Decimal = Decimal("1")) -> ArbitrageScore

# Carbon-aware scheduling
def carbon_intensity_for_provider(provider, *,
                                  source: CarbonIntensitySource | None = None) -> Decimal
def score_carbon_objective(*, cost: Decimal, latency_ms: Decimal,
                           carbon_intensity_gco2_per_kwh: Decimal,
                           weights: CarbonObjectiveWeights) -> CarbonObjectiveScore

# Quality-aware routing
def quality_option_from_scorecard(scorecard: EntityScorecard,
                                  *, model_id: str,
                                  use_normalized_quality: bool = True) -> QualityRoutingOption
def score_quality_routing_option(option: QualityRoutingOption,
                                 *, policy: QualityRoutingPolicy | None = None) -> QualityRoutingScore
def sort_quality_routing_options(options: Iterable[QualityRoutingOption],
                                 *, policy: QualityRoutingPolicy | None = None) -> tuple[QualityRoutingScore, ...]
def select_quality_routing_option(options: Iterable[QualityRoutingOption],
                                  *, policy: QualityRoutingPolicy | None = None) -> QualityRoutingScore

# Provider URL builders
def openai_base_url(provider) -> str
def queue_url(provider, path: str = "") -> str
def lb_url(provider, path: str = "/") -> str
def public_endpoint_url(provider) -> str
def provider_url(provider) -> str
```

---

## 5. Configuration

### Module-level constants (no env vars)

| Constant | Module | Default |
|---|---|---|
| `DEFAULT_LB_MAX_PAYLOAD_MB = 30` | `routing/constraints.py` | Decimal("30") MB for serverless_lb |
| `DEFAULT_FAILURE_THRESHOLD = 3` | `routing/cooldown.py` | consecutive failures before first cooldown |
| `DEFAULT_INITIAL_COOLDOWN = 5min` | `routing/cooldown.py` | `timedelta(minutes=5)` |
| `DEFAULT_ESCALATED_COOLDOWN = 15min` | `routing/cooldown.py` | `timedelta(minutes=15)` |
| `DEFAULT_MAX_ATTEMPTS = 3` | `routing/planner.py` | max fallback attempts |
| `DEFAULT_BACKOFF_BASE_S = 1.0` | `routing/planner.py` | backoff base seconds |
| `DEFAULT_OPENAI_MAX_ATTEMPTS = 3` | `routing/openai.py` | max OpenAI chain attempts |
| `DEFAULT_OPENAI_FALLBACK_BUDGET_S = 5.0` | `routing/fallback.py` | total time budget for OpenAI fallback |
| `DEFAULT_HEDGE_DELAY_S = 0.050` | `routing/hedging.py` | seconds before starting backup attempts |
| `DEFAULT_HEDGED_MAX_ATTEMPTS = 2` | `routing/hedging.py` | total providers a hedged race may start |
| `DEFAULT_HEDGED_MAX_CONCURRENCY = 2` | `routing/hedging.py` | concurrent provider calls in a hedged race |
| `DEFAULT_UNKNOWN_CARBON_INTENSITY_GCO2_PER_KWH = 500` | `routing/carbon.py` | fallback intensity for missing provider/region metadata |
| `DEFAULT_REGION_CARBON_INTENSITIES_GCO2_PER_KWH` | `routing/carbon.py` | static region/datacenter model used by the default carbon source |

No env vars directly control routing. All tuning is via provider record `config` fields
or call-time `lambda_weight`/`carbon_weight` for price×latency×carbon arbitrage.

### Provider config fields that influence routing

| Config key | Purpose |
|---|---|
| `capability_name`, `capability`, `capability_id` | Stage 1 capability |
| `region` | Stage 1 region |
| `enabled`, `health_status` | Stage 2 |
| `cooldown_until`, `consecutive_failures`, `cooldown_trips` | Stage 2 cooldown |
| `priority`, `priority_multiplier` | Stage 3 sort and multiplier |
| `cold_start_p50_ms`, `warm_workers`, `cost_per_second_active`, `recent_error_rate` | Stage 3 formula |
| `gpu_class`, `gpu_type`, `gpu_classes`, `gpu_type_priority` | Stage 1 GPU class |
| `allowed_cuda_versions`, `cuda_min`, `cuda_version` | Stage 1 CUDA |
| `max_payload_mb` | Stage 1 payload limit |
| `fallback_chain`, `fallback_provider_ids`, `fallbacks`, `fallback_for` | Fallback chain construction |
| `runpod_endpoint_id`, `endpoint_id` | URL derivation; SSRF-validated |
| `openai_base_url` | URL override |
| `dataCenterIds`, `datacenter`, `cloud_type`, `gpu_count` | Stage 4 capacity keys |
| `region`, `datacenter`, `dataCenterId`, `dataCenterIds`, `data_center_id`, `data_center_ids`, `datacenter_id`, `dc_id` | Carbon-intensity region lookup |

---

## 6. Failure Modes & Error Types

| Condition | Error | Location |
|---|---|---|
| `max_attempts < 1` | `ValueError` | `planner.py` |
| `backoff_base_s` not finite/negative | `ValueError` | `planner.py` |
| `now` without timezone | `ValueError` | `routing/context.py`, `planner.py`, `resolver/service.py` |
| `availability_cache` ≠ `capacity_cache` | `ValueError` | `planner.py` |
| `context` combined with `now` or cache args | `ValueError` | `planner.py`, `resolver/service.py` |
| Provider missing `id` | `ValueError` | `planner.py` |
| Prewarm `now`/sample timestamp without timezone, negative count, invalid policy sizing/durations | `ValueError` | `prewarm.py` |
| `primary_provider_id` not in candidates | `ValueError` | `openai.py:357` |
| `max_attempts` < 1 or boolean | `ValueError` | `openai.py:363` |
| Invalid hedging options or provider ids | `ValueError` | `hedging.py` |
| All hedged providers fail | `HedgedProviderError` | `hedging.py` |
| Invalid cascade tiers/options/gate result | `ValueError` | `cascade.py` |
| No cascade tier passes quality gate | `CascadeRoutingError` | `cascade.py` |
| Non-empty failover identity fields missing, invalid capacity market, naive checkpoint timestamp | `ValueError` / `TypeError` | `failover.py` |
| No on-demand failover target | `ValueError` | `failover.py` |
| Invalid failover target arbitrage input | `ValueError` | `arbitrage.py` via `failover.py` |
| Source/target provider plugin missing or credentials invalid | `ProviderRegistryError` subclasses | `registry.py` via `failover.py` |
| Checkpoint, provision, or resume hook fails | original exception propagates | `failover.py` |
| Invalid canary fraction/bucket/provider ids/metric windows/thresholds | `ValueError` | `canary.py` |
| Empty arbitrage option list, invalid option identity, non-Decimal or negative arbitrage numeric input | `ValueError` | `arbitrage.py` |
| Empty quality option list, no option satisfying caps, invalid identity, non-Decimal/negative quality-routing numeric input, quality outside `[0, 1]`, or all weights zero | `ValueError` | `quality_routing.py` |
| Invalid carbon provider id/region strings, carbon intensity, or carbon objective weights | `ValueError` | `carbon.py`, `arbitrage.py` |
| `runpod_endpoint_id` absent/invalid | `ValueError` | `provider_urls.py:151-155` |
| `pod_lease` OpenAI URL | `ValueError` | `provider_urls.py:44` |
| No provider survives S1+2 | `NoHealthyProviderError` | `service.py` |
| Capability not found | `CapabilityNotFoundError` | `service.py` |
| Capability disabled | `CapabilityDisabledError` | `service.py` |
| Explicit provider not found | `ProviderNotFoundError` | `service.py` |
| Budget exhausted (no headers) | `OpenAIProxyExecutionError` | `fallback.py:161-168` |

---

## 7. Testing

### `tests/property/test_routing_properties.py`
Hypothesis property tests for pure planner invariants (non-pod-lease providers only):
- `test_determinism` — `RoutePlan.to_dict()` stable across identical calls
- `test_partition_every_provider_ranked_xor_eliminated` — no provider lost or double-counted
- `test_attempts_bounded_and_subset_of_ranked` — attempt count ≤ `max_attempts`; attempt ids ⊆ ranked ids
- `test_ranking_non_increasing_and_finite` — scores finite, non-increasing
- `test_disabled_or_unhealthy_never_attempted` — no attempt with `enabled=False` or `health_status=="unhealthy"`
- `test_max_attempts_below_one_raises` — `ValueError`

### `tests/routing/test_prewarm.py`
Hermetic eight-plan test file plus serialization coverage. It verifies:
- Forecast bucketing, positive-trend projection, headroom, and per-capability output
- Determinism under unsorted history/providers
- Validation for naive `now` and negative request counts
- Endpoint-worker recommendations choose the top healthy provider
- Pod-lease recommendations carry template/datacenter/GPU/cloud target shape
- No recommendation when existing warm capacity already covers projected demand
- Disabled, unhealthy, cooling, and unsupported providers are filtered
- `PrewarmPlan.to_dict()` is JSON-serializable and does not construct RunPod URLs

### `tests/property/test_prewarm_properties.py`
Hypothesis property test: increasing the most recent request window never lowers `projected_requests`.

### `tests/routing/test_canary.py`
Hermetic shadow/canary controller tests. They verify:
- Shadow mode mirrors the candidate while serving baseline output
- Shadow fraction `0.0` does not call the candidate
- Canary mode serves the candidate below the bucket threshold and baseline at the threshold
- Auto-promote returns `PROMOTE` when candidate metrics beat baseline thresholds
- Auto-promote returns `HOLD` on insufficient candidate samples
- Auto-promote returns `ROLLBACK` when success-rate guardrails regress
- Validation rejects invalid traffic fractions, explicit buckets, duplicate provider ids, and invalid observations

### `tests/property/test_canary_properties.py`
Hypothesis property tests for deterministic traffic selection:
- Candidate selection is exactly `bucket < candidate_fraction`
- Selection is monotonic as `candidate_fraction` increases
- Stable traffic buckets are deterministic and always in `[0.0, 1.0)`

### `tests/test_inference_routing.py`
Hermetic API tests. `test_inference_dry_run_routes_by_capability_name_to_priority_one_provider` — with `{unhealthy-p1, healthy-p2, healthy-p1}` selects `prov_priority_1`; verifies `provider_repo.list` with `enabled_only=True`. `test_inference_unknown_explicit_provider_returns_404` — 404 with `"provider_not_found"` for missing `provider_id`.

### `tests/security/test_provider_url_ssrf.py`
SSRF tests. `HOSTILE_IDS` includes `../../../internal`, `169.254.169.254`, `evil.example.com`, `endpoint@evil.com`, `endpoint:8080`, `runpod.ai.evil.com`. `test_lb_url_rejects_hostile_endpoint_id` / `test_queue_url_rejects_hostile_endpoint_id` verify `ValueError` per entry. `test_valid_endpoint_id_targets_only_runpod_host` confirms valid id `"eptest00000000"` → host `eptest00000000.api.runpod.ai`.

### Other files exercising this subsystem
- `tests/conftest.py` — `provider_factory` fixture, `setup_openai_proxy_app` helper
- `tests/routing/test_planning_context.py` — replay determinism, historical availability snapshot, live no-context behavior
- `tests/reconciler/test_init_coverage.py` — patches `is_in_cooldown`; exercises cooldown transitions
- `tests/api/test_inference_contract.py` — imports `CapabilityDisabledError`, `NoHealthyProviderError`, `Stage12Resolution`
- `tests/api/test_e2e_sync_inference.py` — end-to-end with cooldown fields
- `tests/routing/test_hedging.py` — hermetic async hedged racing tests with fake providers, controllable latency/failure/cancellation, bounded fan-out, and validation coverage
- `tests/property/test_hedging_properties.py` — Hypothesis invariant that started attempts never exceed the configured total fan-out bound
- `tests/routing/test_cascade.py` — hermetic async cascade tests for first-tier pass, gate-fail escalation, all-gate-fail metadata, actual cost override, max-attempt caps, sync/async gate support, mapping provider ids, and validation
- `tests/property/test_cascade_properties.py` — Hypothesis invariant that cascade attempts stop at the first gate pass and total cost equals the sum of attempted tier costs
- `tests/routing/test_arbitrage.py` — hermetic price×latency arbitrage tests for
  Decimal scoring, λ=0 cheapest selection, high-λ fastest selection, λ sweep,
  carbon-weight sweep, deterministic tie-breaks, and validation
- `tests/property/test_arbitrage_properties.py` — Hypothesis invariants that the
  selected objective is minimal and selected latency never increases as λ grows
- `tests/routing/test_carbon.py` — hermetic carbon-aware scheduling tests for
  static provider/region overrides, region/default fallback, provider mapping
  region extraction, `dataCenterIds` extraction, weighted objective components,
  and validation
- `tests/property/test_carbon_properties.py` — Hypothesis invariant that with
  equal cost and latency, increasing `carbon_weight` never increases the
  selected carbon intensity
- `tests/routing/test_failover.py` — hermetic eight-plan failover tests for
  preempt→checkpoint→on-demand-resume, no-op on running status, raw preemption
  markers, plain failed non-preemption, on-demand-only target filtering, lambda
  latency tradeoff, no-on-demand validation, and checkpoint-failure stop-before-provision.
- `tests/property/test_failover_properties.py` — Hypothesis invariant that selected
  failover targets are on-demand and have objective no greater than every on-demand
  candidate.
- `tests/routing/test_quality_routing.py` — hermetic quality-aware routing tests
  for cap filtering, default highest-quality selection, weighted blend
  selection, deterministic tie-breaks, scorecard candidate conversion, empty/no
  eligible input, and validation
- `tests/property/test_quality_routing_properties.py` — Hypothesis invariants
  that default selection never lowers quality and weighted selection maximizes
  objective across candidates
- `tests/chaos/test_serverless_5xx.py`, `tests/chaos/test_serverless_429.py` — OpenAI proxy fallback
- `tests/perf/test_micro_benchmarks.py` — `plan_route` benchmarks

---

## 8. Dependencies

**From `pitwall.core`**: `ProviderType` enum; `Capability`, `Provider` models.

**From `pitwall.runpod_client`**: `AvailabilityCache`, `get_global_availability_cache` — `PlanningContext.live()` snapshots current RunPod availability before planning.

**Internal routing imports**:
- `planner` → `context.PlanningContext`, `constraints.filter_hard_constraints`, `cooldown.is_in_cooldown`, `scoring.explain_score`
- `prewarm` → `cooldown.is_in_cooldown`
- `fallback` → `openai.openai_base_url_for_provider`, `openai_base_url`, `build_openai_url`
- `hedging` → generic provider objects plus caller-supplied async provider callable
- `cascade` → generic provider objects plus caller-supplied async provider callable, quality gate, and optional result-cost callable
- `failover` → `ProviderRegistry`, provider `status`/`provision` protocol objects, caller-supplied checkpoint/resume hooks, and `arbitrage.score_arbitrage_option`
- `canary` → generic provider objects plus caller-supplied async provider callable, optional observation callback, and aggregate metric windows
- `arbitrage` → `Decimal` provider/GPU option prices and caller-supplied latency estimates
- `quality_routing` → `Decimal` provider/model quality-cost-latency candidates
  and optional `observability.scorecards.EntityScorecard` conversion
- `openai` → `cooldown.is_in_cooldown`
- `resolver.service` → `PlanningContext`, `routing` types and constraints

**External libs**: `httpx` (async HTTP in fallback); standard library: `asyncio`, `datetime`, `decimal.Decimal`, `math`, `enum.Enum`, `dataclasses`, `collections.abc`, `time`.
