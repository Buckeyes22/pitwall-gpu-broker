# Cost / Budget Subsystem

## 1. Purpose & Scope

The cost subsystem sits between the API layer and RunPod calls. It estimates every
workload's USD cost before it is admitted, gates admission against a monthly budget
and a per-request cap, persists workload records to Postgres, exports Prometheus
metrics, and dispatches threshold notifications through a log-default notifier seam.
Resend email delivery requires `RESEND_API_KEY`, sender/recipient env, and the
optional `email` extra (`src/pitwall/cost/notifications.py:67`, `src/pitwall/cost/notifications.py:74`, `src/pitwall/cost/notifications.py:84`, `pyproject.toml:59`).

## 2. Components

### `estimator.py` — Tagged Pricing Model + Compatibility Estimator

Responsibility: translate a `Capability` + provider cost profile + request payload into
a `Decimal` USD estimate or pre-spend upper bound.

**Key types / functions:**

- `PricingModel` — Pydantic discriminated union keyed by `kind`.
  - `gpu_hour` / `GpuHourPricing` — the current RunPod path. Existing provider records still use `per_second_active`; the tagged model multiplies that active-second rate by `execution_timeout_ms / 1000`.
  - `per_request` / `PerRequestPricing` — flat per-invocation compatibility variant.
  - `per_second` / `PerSecondPricing` — compute time × `rate_per_second`, with optional `bid_rate_per_second`; `upper_bound()` uses the larger of the actual rate and bid rate.
  - `per_token` / `PerTokenPricing` — split prompt/completion token pricing with `per_million_input_tokens` and `per_million_output_tokens`.
  - `per_vm_second` / `PerVmSecondPricing` — flat VM-second rate for VM providers.
- `PricingModelProtocol` — every variant implements `estimate(capability, payload) -> Decimal` and `upper_bound(capability, payload) -> Decimal`.
- `CostQuote` — binds one tagged pricing model to a `Capability` and payload; exposes zero-argument `estimate()` and `upper_bound()` for callers that do not know the variant.
- `parse_pricing_model(provider_cost, cost_mode)` — accepts tagged provider cost maps (`{"kind": ...}` or `{"model": ...}`) and legacy untagged maps. Legacy maps are converted by `cost_mode`:
  - `per_second` → `GpuHourPricing(per_second_active=...)`
  - `per_request` → `PerRequestPricing(per_request=...)`
  - `per_token` → `PerTokenPricing(per_million_input_tokens=..., per_million_output_tokens=...)`
- `quote_cost(capability, provider_cost, payload) -> CostQuote` — public binder for admission.
- `CostEstimator` / `PerSecondEstimator` / `PerRequestEstimator` / `PerTokenEstimator` — compatibility wrappers. They parse provider cost into a tagged variant, then call `estimate()` or `upper_bound()` polymorphically.
- `PerTokenPricing`
  - falls back to `input_bytes / 4` or text-length heuristic (`_estimate_input_tokens`) when tokens are not explicit in payload
  - output tokens default to 256 when `max_tokens`/`max_output_tokens`/`max_completion_tokens`/`max_new_tokens` are absent
  - `estimate()` uses explicit completion tokens when present
  - `upper_bound()` uses the request's max-token ceiling when present, so the budget gate can reserve against the worst allowed completion before the provider has returned usage
- `get_estimator(mode: CostMode) -> CostEstimator` — legacy registry lookup; raises `ValueError` for unknown modes
- `_usd(value: Decimal) -> Decimal` — **quantization**: `value.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)` where `_USD_QUANTUM = Decimal("0.000001")` (6 decimal places)
- `_cost_mapping(provider_cost)` — normalises a flat, nested, or model-like cost dict

Invariant: every non-trivial cost path routes through `_usd`, so all published estimates are quantized to 6 decimal places.

---

### `simulator.py` — What-If Simulator

Responsibility: replay the pure routing planner against a frozen or hypothetical
world and project cost/budget impact without touching Postgres, Redis, RunPod, or
the production availability cache.

**Key types / functions:**

- `WhatIfSimulator(context, price_overrides, budget_usd, current_spend_usd)` — pure
  in-memory simulator. The constructor stores a frozen `PlanningContext`; every
  `simulate()` call replays `plan_route(..., context=context)` and then quotes the
  planned attempts through `quote_cost()`.
- `WhatIfSimulator.from_inputs(now, providers, capability, availability_entries, ...)`
  — convenience constructor for FinOps callers that have raw hypothetical provider,
  capability, price, and capacity inputs. It builds a `PlanningContext.replay(...)`
  snapshot rather than consulting live state.
- `WhatIfWorkload(request, payload)` — one candidate workload for batch simulation.
- `ProviderCostProjection` — per-attempt quote with `provider_id`, `attempt`,
  `estimate_usd`, `upper_bound_usd`, `pricing_kind`, and `selected`.
- `WhatIfProjection` — single-workload output: the full `RoutePlan`, all attempt
  quote rows, the selected-provider reservation, `projected_spend_usd`,
  `budget_headroom_usd`, and `would_exceed_budget`.
- `WhatIfBatchProjection` — ordered batch output. Each workload is projected using
  the running spend from the previous workload, so headroom reflects cumulative
  hypothetical demand.

**Price override behavior:**

- `price_overrides` is keyed by provider id. Values may be legacy cost maps
  (`{"per_second_active": "0.001"}`), tagged pricing maps
  (`{"kind": "per_second", "rate_per_second": "0.001"}`), or provider-shaped maps
  with nested `cost` / `config.cost`.
- Overrides are applied to a copied provider snapshot before replay. The original
  `PlanningContext` remains immutable and reusable.
- For per-second-compatible overrides, the simulator also updates the provider's
  scoring cost fields (`cost_per_second_active` / `per_second_active`) in the copied
  snapshot so `Hints(cost_sensitive=True)` re-ranks providers under the hypothetical
  price curve.

**Budget behavior:**

- Reservation mirrors `BudgetGate.try_launch`: budget impact is the selected
  provider's `CostQuote.upper_bound()`, not the sum of the fallback chain.
- Fallback attempt quotes are still reported so FinOps users can inspect alternate
  provider costs.
- `budget_headroom_usd = budget_usd - projected_spend_usd` is signed; negative
  headroom means the hypothetical workload or batch would exceed the budget.
- All money values are `Decimal` and quantized to 6 decimal places for parity with
  estimator output and persisted USD columns.

Invariant: the simulator is deterministic and performs no I/O. Identical
`PlanningContext`, request, payload, price overrides, budget, and planner options
produce byte-identical `WhatIfProjection.to_dict()` output.

---

### `finops/time_machine.py` - Counterfactual Time-Machine Reports

Responsibility: productize historical routing replay by comparing recorded
actual decisions against one or more counterfactual scenarios. The time machine
is pure: callers provide a frozen `PlanningContext`, historical workload
decisions, and scenario overrides; it performs no database writes, provider
mutations, network calls, or live availability-cache reads.

**Key types / functions:**

- `HistoricalRoutingDecision(workload_id, request, actual_provider_id,
  actual_cost_usd, payload, actual_fallback_chain)` - one persisted workload
  decision. `actual_cost_usd` is the recorded/provider-actual amount supplied by
  reconciliation or workload history, not recomputed from current provider data.
- `CounterfactualScenario(scenario_id, ...)` - named override set. Supported
  knobs include simulator `price_overrides`, provider config overrides,
  removed/added providers, availability snapshots/entries, routing hints,
  observed metrics, budget/current-spend context, `max_attempts`, and
  `backoff_base_s`.
- `TimeMachineReplay(historical_context).replay(scenario, decisions)` - builds a
  scenario-specific `PlanningContext.replay(...)`, calls
  `WhatIfSimulator.simulate(...)` for each historical decision, and emits a
  `TimeMachineReport`.
- `TimeMachineWorkloadReport` - per-workload actual vs counterfactual
  comparison: actual provider/chain/cost, counterfactual provider/chain/cost,
  signed `cost_delta_usd = counterfactual_cost_usd - actual_cost_usd`,
  `routed_differently`, and the underlying `WhatIfProjection`.
- `TimeMachineSummary` - aggregate workload count, changed-route count,
  unroutable count, actual/counterfactual total USD, signed total delta, and
  deterministic provider-count maps.

**Override behavior:**

- Provider overrides are deep-merged into copied provider snapshots. The
  original historical `PlanningContext` and caller-owned override maps remain
  immutable and reusable.
- Price overrides are delegated to `WhatIfSimulator`, including pricing-union
  parsing and cost-sensitive score-field updates.
- Availability overrides use `AvailabilitySnapshot` / `availability_entries` and
  therefore replay Stage 4 capacity decisions without consulting the live RunPod
  cache.
- Report serialization intentionally lists override provider ids rather than
  dumping full provider configs or request payloads, so exported reports do not
  accidentally expose environment values.

Invariant: all cost fields and deltas are `Decimal` values quantized to 6
places. Identical historical context, decisions, and scenario inputs produce
byte-identical `TimeMachineReport.to_dict()` output.

---

### `budget_gate.py` — Postgres-Backed Admission Gate

Responsibility: atomic cost admission with advisory locking and idempotency support.

**Key types / functions:**

- `BudgetGate(pool, monthly_budget_usd, per_request_max_usd, workload_id_factory)` (`budget_gate.py:76`)
  - `monthly_budget_usd` — loaded from env `PITWALL_MONTHLY_BUDGET_USD` if not provided
  - `per_request_max_usd` — loaded from env `PITWALL_PER_REQUEST_MAX_USD` if not provided
  - defaults require the env vars to be set
- `BudgetSnapshot` (`budget_gate.py:24`) — frozen dataclass with `model_dump(mode="json")` and `to_serializable_dict` for HTTP bodies
- `BudgetRejected(RuntimeError)` (`budget_gate.py:52`) — `error_code = "budget_rejected"`, `status_code = 402`; `to_response_body()` returns `{"error", "reason", "snapshot"}`
- `PITWALL_BUDGET_LOCK_KEY = int.from_bytes(b"PITWBUDG", "big")` = `546840836487` (`budget_gate.py:16`) — Postgres advisory lock key
- `BudgetGate.try_launch(capability_id, provider_id, estimate_usd, workload_type, submitted_at, idempotency_key) -> str` — see §4. `estimate_usd` can be a raw `Decimal` or quote-like object with `upper_bound() -> Decimal`; quote objects are converted to the upper bound before per-request/monthly checks and before insert.
- `BudgetGate.current_mtd_spend() -> Decimal` (`budget_gate.py:102`) — read-only month-to-date sum, no lock taken

Invariant: `estimate_usd` and all config decimals must be **strictly positive** (`_positive_decimal` / `_positive_estimate`); zero or negative values raise `ValueError`.

---

### `sync_gate.py` — Synchronous Inference Wiring

Responsibility: the pre-RunPod pipeline — estimate → budget gate → record `queued` → call RunPod → update ledger state.

**Key functions:**

- `gate_sync_inference(capability, provider_id, provider_cost, payload, budget_gate, runpod_caller, idempotency_key, submitted_at, input_bytes, fallback_chain) -> SyncInferenceResult` (`sync_gate.py:125`)
- `estimate_cost(capability, provider_cost, payload) -> Decimal` (`sync_gate.py:236`) — public wrapper
- `SyncInferenceRejected(RuntimeError)` (`sync_gate.py:105`) — wraps a `BudgetRejected` for the sync path
- `update_workload_fallback_chain(pool, workload_id, fallback_chain)` (`sync_gate.py:339`)
- `_MARK_WORKLOAD_RUNNING_SQL` / `_MARK_WORKLOAD_TERMINAL_SQL` / `_MARK_WORKLOAD_ACTIVE_AFTER_CALL_SQL` / `_MARK_WORKLOAD_FAILED_SQL` (`sync_gate.py:39–87`) — SQL fragments that update workload state with timing, bytes, result, and error fields

Invariant: every RunPod call is wrapped so that `failed` / `completed` / `queued` / `running` state is always written back to `pitwall.workloads` even if the caller throws.

---

### `usage.py` — Token Usage Parser

Responsibility: extract actual `prompt_tokens`, `completion_tokens`, `total_tokens` from OpenAI-compatible responses.

**Key functions:**

- `TokenUsage(prompt_tokens, completion_tokens, total_tokens)` (`usage.py:23`) — frozen dataclass
- `parse_usage_json(body: dict) -> TokenUsage | None` (`usage.py:32`) — reads `body["usage"]`
- `parse_usage_sse(raw: bytes | str) -> TokenUsage | None` (`usage.py:63`) — SSE `data:` frame scanner; last frame with usage wins

Invariant: `total_tokens` is derived as `prompt + completion` when not explicitly present (`usage.py:54`).

---

### `threshold_alerts.py` — Threshold Crossing Evaluation

Responsibility: evaluate which configured thresholds the current month's spend has newly crossed, record them, and dispatch notification requests through the notifier seam.

**Key constants:**

- `DEFAULT_THRESHOLDS = (50, 75, 90)` (`threshold_alerts.py:17`)

**Key functions:**

- `evaluate_crossings(pool, budget_usd, thresholds, now) -> list[ThresholdCrossing]` (`threshold_alerts.py:28`)
  - queries `pitwall.workloads` for `COALESCE(SUM(cost_actual_usd), 0)` for the current UTC month
  - excludes thresholds already recorded in `pitwall.alert_events` for this month
  - returns `list[ThresholdCrossing]` ordered by threshold value
- `record_crossings(pool, crossings, now)` (`threshold_alerts.py:98`) — inserts into `pitwall.alert_events` with `ON CONFLICT (month, threshold_pct) DO NOTHING`
- `send_crossing_notifications(crossings) -> list[NotificationResult]` (`src/pitwall/cost/threshold_alerts.py:128`) — imports `send_threshold_email` and calls it per crossing; `send_threshold_email` calls `(notifier or get_notifier()).send(...)` (`src/pitwall/cost/threshold_alerts.py:139`, `src/pitwall/cost/threshold_alerts.py:143`, `src/pitwall/cost/notifications.py:177`)

Invariant: a threshold is only sent once per calendar month (enforced by the `alert_events` dedup check).

---

### `alerts.py` — 80 % Budget Alert with Redis Dedup

Responsibility: check whether cumulative month-to-date spend has crossed 80 % of the monthly budget and dispatch a single notification per month through the notifier seam, deduped via Redis.

**Key constants:**

- `_BUDGET_ALERT_KEY_PREFIX = "pitwall:budget-alert"` (`src/pitwall/cost/alerts.py:26`)
- `_BUDGET_ALERT_TTL_SECONDS = 45 * 24 * 60 * 60` = 3 888 400 s (`src/pitwall/cost/alerts.py:27`)

**Key functions:**

- `check_and_send_budget_alert(pool, redis_client, *, now, http_client, notifier) -> BudgetAlertResult` (`src/pitwall/cost/alerts.py:46`)
  - `budget_usd` from env `PITWALL_MONTHLY_BUDGET_USD` (`src/pitwall/cost/alerts.py:24`, `src/pitwall/cost/alerts.py:171`)
  - computes `budget_pct = mtd_spend / budget_usd * 100` (`src/pitwall/cost/alerts.py:89`)
  - only dispatches when `budget_pct >= 80` (`src/pitwall/cost/alerts.py:91`)
  - Redis key: `pitwall:budget-alert:YYYY-MM:80` (`src/pitwall/cost/alerts.py:103`)
- `_compute_mtd_spend(pool, current_time) -> Decimal` (`src/pitwall/cost/alerts.py:157`)
- `_send_budget_notification(mtd_spend, budget_usd, budget_pct, notifier)` (`src/pitwall/cost/alerts.py:178`) — calls `(notifier or get_notifier()).send(...)` (`src/pitwall/cost/alerts.py:193`)

---

### `notifications.py` — Alert Notification Seam

Responsibility: format alert payloads and route them through `Notifier`. The default transport logs; Resend is selected only by configuration and imported lazily.

**Key types / functions:**

- `NotificationResult(threshold_pct, email_id, error, ok)` (`src/pitwall/cost/notifications.py:26`)
- `Notifier` (Protocol, `src/pitwall/cost/notifications.py:34`) — `send(subject, body) -> NotificationResult`
- `LogNotifier` (`src/pitwall/cost/notifications.py:39`) — default transport; writes a structured alert log line and returns `ok=True` (`src/pitwall/cost/notifications.py:42`)
- `ResendNotifier` (`src/pitwall/cost/notifications.py:47`) — optional transport; imports `resend` inside `send` (`src/pitwall/cost/notifications.py:94`)
- `get_notifier() -> Notifier` (`src/pitwall/cost/notifications.py:119`) — returns `ResendNotifier` when `RESEND_API_KEY` is set; otherwise returns `LogNotifier` (`src/pitwall/cost/notifications.py:121`, `src/pitwall/cost/notifications.py:123`)
- `send_threshold_email(crossing, notifier=None) -> NotificationResult` (`src/pitwall/cost/notifications.py:164`) — builds threshold text and calls `(notifier or get_notifier()).send(...)` (`src/pitwall/cost/notifications.py:174`, `src/pitwall/cost/notifications.py:177`)

Invariant: the base broker has no `resend` dependency. `resend` lives in the optional `email` extra (`pyproject.toml:58`, `pyproject.toml:59`).

---

### `hibernate_alerts.py` — L14 LB Hibernate Sweep Alerts

Responsibility: alert when an L14 Load Balancer endpoint has `workersMin > 0` but is not hibernated, indicating wasted idle cost.

**Key constants:**

- `L14_DAILY_BURN_PER_WORKER_USD = 100.0` (`src/pitwall/cost/hibernate_alerts.py:23`)

**Key types / functions:**

- `HibernateSweepAlert(provider_id, provider_name, endpoint_id, workers_min, duration_hours, burn_estimate_usd)` (`src/pitwall/cost/hibernate_alerts.py:26`)
- `HibernateAlertResult(provider_id, endpoint_id, email_id, error)` (`src/pitwall/cost/hibernate_alerts.py:38`)
- `send_hibernate_sweep_alert(alert, http_client, notifier) -> HibernateAlertResult` (`src/pitwall/cost/hibernate_alerts.py:67`)
  - calls `(notifier or get_notifier()).send(...)`; `http_client` is deprecated compatibility (`src/pitwall/cost/hibernate_alerts.py:77`, `src/pitwall/cost/hibernate_alerts.py:92`)

---

### `exporter.py` — Prometheus Cost Exporter

Responsibility: HTTP `/metrics` endpoint (port 9109) exposing Prometheus gauges for cloud spend, budget %, active workers, kill-log triggers, and unhealthy providers.

**Gauges:**

| Name | Description |
|------|-------------|
| `pitwall_cloud_spend_month_usd` | Cumulative monthly spend |
| `pitwall_cloud_budget_pct` | Spend as % of budget |
| `pitwall_cloud_budget_usd` | Monthly budget |
| `pitwall_active_workers{provider}` | Active lease count per provider |
| `pitwall_kill_log_triggers_7d` | Kill-switch activations in last 7 days |
| `pitwall_providers_unhealthy` | Count of providers with `health_status = 'unhealthy'` |

Poll interval: 60 s. Runs as `python -m pitwall.cost` or via `main()`.

---

### `billing_read.py` — RunPod Billing Read + Budget Reconciliation

Responsibility: read-only typed access to RunPod account credit balance and spend metadata via the GraphQL client, plus reconciliation helpers that compare provider-reported numbers against Pitwall's internal budget gate state.

**Key types / functions:**

- `BillingSnapshot(user_id, client_balance_usd, current_spend_per_hr_usd, spend_limit_usd, min_balance_usd, under_balance)` (`src/pitwall/cost/billing_read.py:21`) — frozen dataclass built from `RunpodCreditsBalance`; all money fields are `Decimal`
- `BillingSnapshot.from_runpod(balance: RunpodCreditsBalance)` (`src/pitwall/cost/billing_read.py:32`) — factory from the GraphQL model
- `BillingSnapshot.to_serializable_dict()` (`src/pitwall/cost/billing_read.py:43`) — stdlib-JSON-safe dict with `Decimal` values as strings
- `BudgetReconciliation(...)` (`src/pitwall/cost/billing_read.py:68`) — frozen dataclass comparing RunPod and Pitwall budget numbers; includes `variance_usd` = `runpod_balance - pitwall_budget_remaining`
- `BudgetReconciliation.to_serializable_dict()` (`src/pitwall/cost/billing_read.py:90`) — stdlib-JSON-safe dict
- `BudgetGateLike` (Protocol, `src/pitwall/cost/billing_read.py:114`) — minimal protocol requiring `monthly_budget_usd: Decimal` and `async current_mtd_spend() -> Decimal`; `BudgetGate` satisfies this protocol
- `read_billing_snapshot(client: RunpodGraphQLClient) -> BillingSnapshot` (`src/pitwall/cost/billing_read.py:121`) — thin async wrapper around `client.credits_balance()`
- `reconcile_with_budget(client, budget_gate) -> BudgetReconciliation` (`src/pitwall/cost/billing_read.py:134`) — fetches RunPod billing state, reads Pitwall MTD spend, and computes variance

Invariant: all money fields use `Decimal`; floats are never accepted or produced. GraphQL response JSON is decoded with `parse_float=Decimal` (handled by the underlying `RunpodGraphQLClient`).

---

### `reconcile_cost.py` — Provider Billing Truth-Up

Responsibility: compare broker-recorded spend against provider-actual billing at the `pitwall.cost_daily` window grain and emit deterministic ledger corrections.

**Key types / functions:**

- `CostReconcileWindow(day, capability_class, provider_type)` — one `cost_daily` key.
- `RecordedCostWindow(window, recorded_usd, workload_count)` — broker-recorded cost from `pitwall.cost_daily`.
- `ProviderActualCostWindow(window, actual_usd, source)` — provider-billing actual for the same window, e.g. RunPod billing or a provider adapter.
- `CostReconcileAdjustment(window, recorded_usd, provider_actual_usd, adjustment_usd, sources)` — signed correction where `adjustment_usd = provider_actual_usd - recorded_usd`; positive means increase the ledger, negative means decrease it.
- `CostReconcilePlan(adjustments, window_count)` — deterministic result with `total_adjustment_usd`, `adjustment_count`, and `to_serializable_dict()`.
- `reconcile_cost(recorded, provider_actuals, tolerance_usd=Decimal("0.000000")) -> CostReconcilePlan` — pure truth-up; groups duplicate windows, compares the recorded/provider union, drops differences within tolerance, and sorts output by window.
- `AsyncpgCostTruthUpRepository(pool)` — thin adapter that reads recorded windows from `pitwall.cost_daily` for `[start_day, end_day)` and applies adjustments by upserting `cost_usd = provider_actual_usd`.
- `truth_up_cost_daily(repository, start_day, end_day, provider_actuals, tolerance_usd)` — orchestration helper that fetches recorded windows, computes the plan, applies it, and returns the emitted plan.

Invariant: all money inputs must be `Decimal`, finite, non-negative before comparison, and quantized to 6 decimal places with `ROUND_HALF_UP`. The pure function is deterministic for identical inputs. The asyncpg adapter is idempotent because it sets the window's `cost_usd` to the provider actual instead of adding the correction delta repeatedly.

---

### `circuit_breaker.py` — Budget Circuit Breaker / Auto-Downgrade

Responsibility: stateful circuit breaker that trips when budget headroom or burn-rate runway crosses configurable thresholds, emits `allow` / `downgrade` / `block` decisions, and recovers with hysteresis to avoid flapping.

**Key types / functions:**

- `BudgetCircuitBreaker` (`circuit_breaker.py:54`) — stateful breaker with configurable thresholds
  - `headroom_trip_pct` — default `10.0`; trips closed→open when headroom % falls at or below this value
  - `runway_trip_hours` — default `24.0`; trips when burn-rate runway (hours until exhaustion) falls at or below this value
  - `recovery_headroom_pct` — default `20.0`; must be **>** `headroom_trip_pct` (hysteresis)
  - `recovery_runway_hours` — default `72.0`; must be **>** `runway_trip_hours` (hysteresis)
  - `downgrade_headroom_pct` — default `5.0`; below this, the breaker emits `block` instead of `downgrade`
  - `cooldown_seconds` — default `300.0`; time before an `open` breaker transitions to `half-open`
- `CircuitBreakerDecision` (`circuit_breaker.py:29`) — frozen dataclass with `action`, `reason`, `state`, `headroom_usd`, `headroom_pct`, `runway_hours`
- `BudgetCircuitBreaker.evaluate(budget_usd, mtd_spend_usd, now, burn_rate_usd_per_hour)` (`circuit_breaker.py:76`) — explicit *now* for determinism; requires timezone-aware datetime
- `BudgetCircuitBreaker.state` — read-only current state (`closed` / `open` / `half-open`)
- `BudgetCircuitBreaker.reset()` — resets to `closed`; useful for testing

**State machine:**

| Transition | Condition | Action emitted |
|------------|-----------|----------------|
| `closed` → `open` | `headroom_pct <= headroom_trip_pct` OR `runway_hours <= runway_trip_hours` | `downgrade` or `block` |
| `open` → `half-open` | `now - last_trip_at >= cooldown_seconds` | `downgrade` or `block` |
| `half-open` → `closed` | `headroom_pct >= recovery_headroom_pct` AND `runway_hours >= recovery_runway_hours` | `allow` |
| `half-open` → `open` | Recovery thresholds NOT met | `downgrade` or `block` |

**Downgrade vs block:**

- `block` when `headroom_pct <= downgrade_headroom_pct` (budget effectively exhausted)
- `downgrade` when tripped but headroom is still above the downgrade threshold
- `allow` when `closed`

The gate (e.g. `sync_gate.gate_sync_inference`) can consult the breaker after estimating cost and before calling `BudgetGate.try_launch`.  A `downgrade` decision can be translated into `Hints(cost_sensitive=True)` for the planner; a `block` decision can short-circuit to HTTP 402 without touching Postgres.

Invariant: the breaker is deterministic.  Identical inputs plus identical internal state always yield identical `CircuitBreakerDecision` objects.

---

### `budget_kill_escalation.py` — Budget-Breach → Kill-Switch Escalation (opt-in)

Responsibility: the **most severe** rung of the budget ladder — optionally auto-firing the
operator kill switch (`CloudKillSwitch`, network sever + compute termination) when the budget is
*exhausted*, not merely low. Because auto-terminating running compute on a budget number is
dangerous, it is **disabled by default** and gated three ways.

**Escalation ladder (increasing severity):**

    per-run cap reject → monthly cap reject → circuit-breaker `downgrade` →
    circuit-breaker `block` → **budget-breach kill escalation** (this module)

**Modes** (`PITWALL_BUDGET_BREACH_KILL_MODE`):

- `disabled` (default) — inert; never evaluates or fires.
- `shadow` — evaluates the trigger and **logs what it would do**, but never touches the kill
  switch. Lets operators validate the trigger before arming.
- `armed` — fires `CloudKillSwitch.activate(...)` when the trigger holds.

**Trigger gate** (must *all* hold): mode ≠ `disabled`; circuit-breaker `action == "block"`
(unless `KillEscalationPolicy.require_block` is relaxed); and headroom ≤
`PITWALL_BUDGET_BREACH_KILL_HEADROOM_FLOOR_USD` (default `0` — i.e. fully exhausted/overrun).
Never escalates on `allow`/`downgrade`.

**Key types / functions:**

- `KillEscalationMode` — `"disabled" | "shadow" | "armed"`
- `KillEscalationPolicy` — frozen: `require_block` (default True), `headroom_floor_usd` (default 0)
- `evaluate_kill_escalation(decision, *, mode, policy)` — **pure/deterministic** decision
- `maybe_escalate_to_kill(decision, kill_switch, *, mode, policy)` — async invoker; the *only* thing
  that can fire the switch; returns a `KillEscalationOutcome` audit record (`fired`, `mode`, `reason`,
  `report`) in every mode
- `KillSwitchLike` — minimal Protocol (`async activate(reason)`) keeping this module decoupled from
  `pitwall.api.admin` (no circular import)

**Integration:** wire `maybe_escalate_to_kill` at the circuit-breaker decision consumption point,
passing the configured mode/policy and a `CloudKillSwitch`. Off-by-default means existing deployments
are unaffected until an operator opts in (recommended path: `shadow` first, then `armed`).

### `slo_governor.py` — Cost SLO Governor / Spend-Velocity Pacing

Responsibility: evaluate spend velocity against configurable cost SLOs and emit pacing decisions (`allow` / `throttle` / `defer`) without duplicating the breaker's absolute-budget protection.

**Key types / functions:**

- `CostSLO` (`slo_governor.py:23`) — Pydantic frozen model defining
  - `per_day_target_usd` — daily spend target (positive)
  - `per_request_p95_usd` — optional per-request cost p95 target
  - `throttle_threshold` — default `0.80`; velocity ratio at which to throttle
  - `defer_threshold` — default `1.00`; velocity ratio at which to defer
  - validates `throttle_threshold < defer_threshold` and all money fields positive
- `GovernorDecision` (`slo_governor.py:48`) — frozen dataclass with `action`, `reason`, `velocity_ratio`, `request_p95_ratio`, `slo`, `breaker_action`
- `CostGovernor` (`slo_governor.py:72`)
  - `evaluate(slo, burn_rate_usd_per_day, now, breaker_decision, recent_request_costs_usd)` — deterministic; explicit *now* required (timezone-aware)
  - Computes `velocity_ratio = burn_rate_usd_per_day / per_day_target_usd`
  - Optional per-request p95 analysis when `recent_request_costs_usd` and `per_request_p95_usd` are both supplied
  - Composes with breaker: `block` → `defer`, `downgrade` → at least `throttle`

**Pacing logic:**

| Velocity ratio | Action | Reason |
|----------------|--------|--------|
| `< throttle_threshold` | `allow` | burn rate within daily SLO target |
| `>= throttle_threshold` and `< defer_threshold` | `throttle` | burn rate approaching daily SLO target |
| `>= defer_threshold` | `defer` | burn rate exceeds daily SLO target |

The breaker composes as an override: a `block` decision always produces `defer`, and a `downgrade` decision escalates `allow` to `throttle` without weakening an existing `defer`.

Invariant: the governor is deterministic.  Identical inputs always yield identical `GovernorDecision` objects.

---

### `__init__.py` — Lazy Module-Level Re-Exports

`pitwall.cost` uses a lazy `__getattr__` (`__init__.py:77`) to import from submodules on first attribute access. The export groups are: `_BUDGET_GATE_EXPORTS`, `_ESTIMATOR_EXPORTS`, `_SYNC_GATE_EXPORTS`, `_THRESHOLD_ALERTS_EXPORTS`, `_USAGE_EXPORTS`, `_BILLING_READ_EXPORTS`, `_SIMULATOR_EXPORTS`, `_CIRCUIT_BREAKER_EXPORTS`, `_SLO_GOVERNOR_EXPORTS`.

---

### `burn_rate.py` — Burn-Rate Forecaster

Responsibility: read-only analytics that project current burn rate and time-to-budget-exhaustion (runway) from a window of daily spend points.  The core forecaster is pure (no I/O); a thin Postgres adapter queries `pitwall.cost_daily`.

**Key types / functions:**

- `SpendPoint(day, cost_usd)` (`src/pitwall/finops/burn_rate.py:22`) — one day of observed spend
- `BurnRateForecast(burn_rate_usd_per_day, projected_exhaustion, trend, confidence, budget_usd, remaining_budget_usd, runway_days)` (`src/pitwall/finops/burn_rate.py:29`) — deterministic projection given a spend window
- `BurnRateForecaster` (`src/pitwall/finops/burn_rate.py:42`)
  - `forecast(points, *, budget_usd, mtd_spend_usd, now) -> BurnRateForecast`
  - `now` is required and must be timezone-aware (normalised to UTC internally)
  - `burn_rate_usd_per_day` = total window cost / calendar day span, quantised to 6 decimal places
  - `trend` is computed by comparing the average of the first half of the window to the second half:
    - `increasing` when second-half average > first-half average × 1.05
    - `decreasing` when second-half average < first-half average × 0.95
    - `stable` otherwise
    - `insufficient_data` when fewer than 2 points
  - `confidence` is a heuristic in [0, 1] based on point count (sqrt(n) / sqrt(7)) and coefficient of variation (1 − cv)
  - `projected_exhaustion` = `now + timedelta(days=runway)`; `None` when burn rate is zero, budget is already exhausted, or the projected date exceeds Python's `datetime` representable range
- `forecast_from_cost_daily(pool, *, budget_usd, mtd_spend_usd, now, window_days=30) -> BurnRateForecast` (`src/pitwall/finops/burn_rate.py:170`) — thin async adapter that reads `pitwall.cost_daily` for the last *window_days* and delegates to `BurnRateForecaster`

Invariant: the core forecaster is deterministic and has no side effects; the adapter is the only I/O path.

### `reservations.py` — Reservation recommender

Responsibility: evaluate on-demand cost against reservation / warm-pool candidate plans and return a recommendation-only FinOps decision. The core path is pure: callers provide a `DemandForecast`, a `WhatIfSimulator`, reservation candidates, and optionally a `BurnRateForecast`; the recommender performs no provider mutations, no database writes, and no network calls.

**Key types / functions:**

- `DemandForecast(name, workloads, window_hours)` — named forecast window containing `WhatIfWorkload` entries that are replayed through the simulator.
- `ReservationLine(provider_id, reserved_units, warm_pool_size, unit_capacity, hourly_commitment_usd, upfront_usd)` — one provider sizing line. `capacity_workloads = (reserved_units + warm_pool_size) * unit_capacity`; fixed cost is upfront cost plus hourly commitment over the forecast window.
- `ReservationCandidate(plan_id, reserves, price_overrides)` — one build/warm-pool plan. `price_overrides` must reference providers present in the candidate reservation lines, so discounted marginal pricing cannot be evaluated without declared capacity.
- `recommend_reservations(demand, simulator, candidates, burn_rate_forecast=None) -> ReservationRecommendation` — computes the on-demand baseline with `WhatIfSimulator.simulate_workloads(...)`, then evaluates each candidate with remaining reserved capacity. Workloads covered by reserved capacity use candidate price overrides; overflow workloads fall back to the on-demand baseline projection.
- `ReservationRecommender(simulator).recommend(...)` — reusable wrapper for callers that want to bind the simulator once.
- `PlanEvaluation` — structured evaluation with fixed, marginal, and total Decimal cost, covered workload count, overflow count, unmet count, selected-provider counts, projected savings, and optional budget-after-plan / runway-after-plan metadata.
- `ReservationRecommendation` — final output with `action` of `reserve`, `on_demand`, or `blocked`, the on-demand baseline, candidate evaluations, the selected evaluation, and JSON-safe `to_dict()`.

Selection rule: only plans with `meets_demand=True` are eligible. The chosen plan is the lowest total cost; exact ties prefer on-demand, then candidate `plan_id` order for deterministic output. If no plan can route the demand, the action is `blocked`.

Invariant: all costs and savings are `Decimal` values quantised to 6 decimal places. Identical demand, simulator context, candidates, and burn-rate forecast produce byte-identical `ReservationRecommendation.to_dict()` output.

### `sub_budgets.py` — Blast-Radius Sub-Budgets + Chargeback

Responsibility: partition the monthly budget into named sub-budgets (per capability/team/tag), gate admission against the relevant sub-budget, and attribute spend via deterministic chargeback reports.

**Key types / functions:**

- `SubBudget(tag, allocation_usd, description)` (`src/pitwall/cost/sub_budgets.py:28`) — one named slice of the monthly budget
- `SubBudgetConfig(total_budget_usd, budgets)` (`src/pitwall/cost/sub_budgets.py:38`) — Pydantic model that validates sub-budget allocations sum ≤ total budget
- `SubBudgetGate(budget_gate, config, tag_mtd_spend)` (`src/pitwall/cost/sub_budgets.py:94`) — wraps `BudgetGate` and adds per-tag admission
  - `tag_mtd_spend` is an optional async callable `tag -> Decimal`; when omitted the gate tracks spend in-memory
  - `try_launch(tag, capability_id, provider_id, estimate_usd, ...)` checks the sub-budget first, then delegates to the underlying `BudgetGate`
  - exposes `monthly_budget_usd` and `current_mtd_spend()` for compatibility with `BudgetGateLike`
- `SubBudgetRejected(RuntimeError)` (`src/pitwall/cost/sub_budgets.py:74`) — `error_code = "sub_budget_rejected"`, `status_code = 402`; reasons include `"unknown_tag"` and `"sub_budget"`
- `SubBudgetSnapshot` (`src/pitwall/cost/sub_budgets.py:58`) — frozen dataclass with tag-specific state and `to_serializable_dict()` for HTTP bodies
- `generate_chargeback_report(config, workloads, tag_resolver)` (`src/pitwall/cost/sub_budgets.py:189`) — pure function that attributes spend by tag
  - prefers `cost_actual_usd` over `cost_estimate_usd` per workload
  - workloads without a matching tag are counted as `unallocated_spend_usd`
  - returns `ChargebackReport(total_spend_usd, line_items, unallocated_spend_usd)` with `to_serializable_dict()`

Invariant: all money values are `Decimal` and validated for finiteness. Sub-budget allocations are non-negative; the total budget is strictly positive. The chargeback report is deterministic: identical `config`, `workloads`, and `tag_resolver` always produce the same report.

---

## 3. Cost Model

### Tagged Pricing Variants

Provider cost now has a tagged shape. The tag lives in `kind` (or `model` for caller convenience), and each variant owns its own math:

| `kind` | Formula | Upper bound behavior | Rate keys |
|--------|---------|----------------------|-----------|
| `gpu_hour` | `per_second_active * (execution_timeout_ms / 1000)` | same as estimate | `per_second_active` |
| `per_request` | flat per-invocation fee | same as estimate | `per_request` |
| `per_second` | `rate_per_second * (execution_timeout_ms / 1000)` | uses `max(rate_per_second, bid_rate_per_second)` when a bid is present | `rate_per_second`, optional `bid_rate_per_second` |
| `per_token` | `(per_million_input_tokens * in_tok + per_million_output_tokens * out_tok) / 1_000_000` | uses max-token ceiling for completion tokens before the request completes | `per_million_input_tokens`, `per_million_output_tokens` |
| `per_vm_second` | `rate_per_second * (execution_timeout_ms / 1000)` | same as estimate | `rate_per_second` |

Legacy untagged provider cost maps still work. `parse_pricing_model()` converts legacy capability modes to tagged variants so current providers keep exact numbers while future providers can supply a tagged map directly.

Token fallback hierarchy (`PerTokenEstimator._estimate_tokens` / `PerTokenPricing`):
- Explicit: `payload["input_tokens"]` / `payload["output_tokens"]` (or nested `payload["usage"]`)
- Heuristic: `input_bytes / 4` or `sum(text_lengths_of("system","messages","prompt","input")) / 4`
- Output default: 256 tokens when `max_tokens` / `max_output_tokens` / `max_completion_tokens` / `max_new_tokens` are absent

Why `upper_bound()` matters: per-token and bid/spot pricing can be open-ended at launch time. The exact cost may depend on completion tokens or the settled provider rate, but the budget gate must decide before dispatch. `CostQuote.upper_bound()` gives the gate a conservative Decimal amount to reserve without knowing the concrete pricing variant.

### Rounding / Quantization

All published estimates pass through `_usd(value)`:

```
estimate_usd = value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
```

`_USD_QUANTUM = Decimal("0.000001")`. Every estimate is therefore accurate to **6 decimal places** (micro-dollar).

## 4. Budget Admission Flow (`BudgetGate.try_launch`)

```
try_launch(capability_id, provider_id, estimate_usd, workload_type,
           submitted_at, idempotency_key)
│
├─ _positive_estimate(estimate_usd)
│   ├─ raw Decimal/string/int/float → _positive_decimal(...)
│   └─ quote-like object → _positive_decimal(estimate_usd.upper_bound(), ...)
│   └─ raise ValueError if estimate <= 0
│
├─ pre-flight: per_request_cap check (outside transaction)
│   └─ if estimate > self.per_request_max_usd
│        → raise BudgetRejected("per_request_cap", snapshot)
│
└─ async with pool.acquire() as conn, conn.transaction():
     ├─ SELECT pg_advisory_xact_lock(PITWALL_BUDGET_LOCK_KEY)   ← exclusive advisory lock
     │
     ├─ if idempotency_key is not None:
     │    SELECT id FROM pitwall.workloads WHERE idempotency_key = $1
     │    → return existing_id if found (idempotent replay)
     │
     ├─ SELECT COALESCE(SUM(cost_estimate_usd), 0)
     │    FROM pitwall.workloads
     │    WHERE submitted_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
     │      AND state IN ('queued','running','completed')
     │   → mtd_spend
     │
     ├─ if mtd_spend + estimate > self.monthly_budget_usd
     │    → raise BudgetRejected("monthly_budget", snapshot)
     │
     ├─ workload_id = _workload_id_factory()  (default: "wkl_" + ULID)
     │
     ├─ INSERT pitwall.workloads (id, capability_id, provider_id, type,
     │   state='queued', cost_estimate_usd, submitted_at[, idempotency_key])
     │   RETURNING id
     │
     └─ return str(admitted_id)
```

Post-insert, callers (e.g. `sync_gate.gate_sync_inference`) immediately transition the row to `running` via `_mark_workload_running`.

## 5. Configuration

| Env Var | Module | Default | Description |
|---------|--------|---------|-------------|
| `PITWALL_MONTHLY_BUDGET_USD` | `src/pitwall/cost/budget_gate.py:91`, `src/pitwall/cost/alerts.py:24`, `src/pitwall/cost/exporter.py:57` | **required** (gate/alerts) / `"1000"` (exporter) | Monthly spend cap |
| `PITWALL_PER_REQUEST_MAX_USD` | `src/pitwall/cost/budget_gate.py:97` | **required** | Per-request estimate cap |
| `RESEND_API_KEY` | `src/pitwall/cost/notifications.py:19`, `src/pitwall/cost/notifications.py:121`, `src/pitwall/cost/notifications.py:123` | unset → `LogNotifier` | Selects `ResendNotifier` when set; absent falls back to structured logging |
| `PITWALL_ALERT_FROM` | `src/pitwall/cost/notifications.py:20`, `src/pitwall/cost/notifications.py:74` | required for `ResendNotifier` | From address; falls back to `RESEND_SENDER_EMAIL` |
| `PITWALL_ALERT_TO` | `src/pitwall/cost/notifications.py:21`, `src/pitwall/cost/notifications.py:84` | required for `ResendNotifier` | Alert recipient; falls back to `RESEND_BUDGET_ALERT_EMAIL` |
| `RESEND_SENDER_EMAIL` | `src/pitwall/cost/notifications.py:22`, `src/pitwall/cost/notifications.py:126` | fallback only | Legacy from address when `PITWALL_ALERT_FROM` is unset |
| `RESEND_BUDGET_ALERT_EMAIL` | `src/pitwall/cost/notifications.py:23`, `src/pitwall/cost/notifications.py:130` | fallback only | Legacy recipient when `PITWALL_ALERT_TO` is unset |
| `PITWALL_COST_EXPORTER_PORT` | `src/pitwall/cost/exporter.py:146` | `"9109"` | Metrics HTTP port |
| `DATABASE_URL` | `src/pitwall/cost/exporter.py:62` | **required** (exporter) | Postgres DSN |

## 6. Failure Modes

| Error | Type | Trigger | HTTP Status |
|-------|------|---------|-------------|
| `BudgetRejected(reason="per_request_cap")` | `RuntimeError` | `estimate > per_request_max_usd` before lock | 402 |
| `BudgetRejected(reason="monthly_budget")` | `RuntimeError` | `mtd_spend + estimate > monthly_budget` under lock | 402 |
| `SyncInferenceRejected(reason, budget_error)` | `RuntimeError` | wraps `BudgetRejected` for the sync path | 402 |
| `ValueError` (from `_positive_decimal`) | `ValueError` | `estimate_usd <= 0` or config not positive | 500 |
| `ValueError` (from `get_estimator`) | `ValueError` | unknown `CostMode` | 500 |
| `ValueError` (from `_required_non_negative_decimal`) | `ValueError` | provider cost missing required key | 500 |
| `ValueError` (from `_usd`) | `ValueError` | cost estimate out of representable USD range | 500 |

`BudgetRejected.to_response_body()` (`budget_gate.py:63`) is the canonical HTTP 402 body:
```json
{"error": "budget_rejected", "reason": "monthly_budget", "snapshot": {...}}
```

## 7. Testing

| Test file | What it covers |
|-----------|----------------|
| `tests/cost/test_estimator.py` | Legacy estimator characterization, tagged pricing variants, quote interface, dispatch |
| `tests/cost/test_simulator.py` | What-if planner replay, price overrides, per-attempt cost breakdown, selected upper-bound budget headroom, batch accumulation |
| `tests/unit/cost/test_estimator_rounding.py` | `_usd` quantization to 6 decimal places |
| `tests/unit/cost/test_estimator_boundary.py` | Zero, negative, missing keys, unknown `CostMode` |
| `tests/unit/cost/test_sync_persist_deadline.py` | Sync gate workload state transitions |
| `tests/cost/test_budget_gate.py` | `BudgetGate.try_launch` admit/reject logic, idempotency, quote upper-bound gating |
| `tests/cost/test_sync_gate.py` | Full `gate_sync_inference` pipeline |
| `tests/cost/test_threshold_alerts.py` | `evaluate_crossings`, `record_crossings` |
| `tests/cost/test_budget_alerts.py` | `check_and_send_budget_alert` with Redis dedup |
| `tests/cost/test_usage.py` | `parse_usage_json`, `parse_usage_sse` |
| `tests/cost/test_hibernate_alerts.py` | `send_hibernate_sweep_alert` |
| `tests/cost/test_cloud_cost_exporter.py` | `/metrics` endpoint |
| `tests/cost/test_billing_read.py` | `read_billing_snapshot`, `reconcile_with_budget`, `BillingSnapshot`, `BudgetReconciliation`; hermetic fake transport; Decimal fidelity; error propagation |
| `tests/cost/test_reconcile_cost.py` | Provider truth-up adjustments, tolerance, window union, duplicate grouping, Decimal quantization/serialization, and asyncpg adapter SQL |
| `tests/unit/finops/test_burn_rate.py` | BurnRateForecaster basics: empty/single/multi-point windows, trend detection, budget exhaustion, gap handling, naive-now guard |
| `tests/property/test_burn_rate_properties.py` | Property-based invariants: non-negative burn rate, remaining budget accuracy, confidence ∈ [0,1], zero-burn behaviour |
| `tests/unit/finops/test_reservations.py` | Reservation recommender: on-demand fallback, warm-pool savings, overflow handling, blocked demand, burn-rate metadata, deterministic serialization, stable tie-break, candidate validation |
| `tests/property/test_reservations_properties.py` | Property-based invariant: selected eligible plan has the minimum total cost |
| `tests/cost/test_circuit_breaker.py` | State transitions (closed/open/half-open), hysteresis, downgrade vs block, runway edge cases, determinism, reset |
| `tests/property/test_circuit_breaker_properties.py` | Hypothesis: valid action/state invariants, block-only-when-headroom-low, runway monotonicity with burn rate |
| `tests/cost/test_sub_budgets.py` | Sub-budget config validation, sub-budget gate admit/reject, chargeback attribution, snapshot serialization |
| `tests/property/test_sub_budget_properties.py` | Property-based invariants: allocation sum ≤ total, chargeback total = parts, remaining non-negative |
| `tests/property/test_reconcile_cost_properties.py` | Property-based invariants: total adjustment equals provider-recorded delta and output is order-invariant |
| `tests/cost/test_slo_governor.py` | Velocity-based pacing (allow/throttle/defer), breaker composition, per-request p95, custom thresholds, validation, determinism |
| `tests/property/test_slo_governor_properties.py` | Hypothesis: valid action invariants, breaker block always defers, velocity monotonicity |
| `tests/integration/test_budget_gate.py` | Concurrent launch, overspend under load |
| `tests/integration/test_budget_overspend_concurrency.py` | Budget race-condition hardening |
| `tests/integration/test_idempotency_key_concurrency.py` | Idempotency key dedup under concurrency |
| `tests/chaos/test_db_outage_fail_closed.py` | Budget gate behaviour when DB is unavailable |
| `tests/test_full_cost_path.py` | End-to-end cost estimation → threshold alert |
| `tests/property/test_simulator_properties.py` | What-if budget headroom monotonicity as selected-provider rate increases |

## 8. Dependencies

**Intra-pitwall imports:**

| Module | Imports from |
|--------|-------------|
| `budget_gate.py` | `pitwall.core.ids.ulid_new` |
| `simulator.py` | `pitwall.routing.{PlanningContext,plan_route}`, `pitwall.cost.estimator.quote_cost`, `pitwall.core.models.Capability` |
| `reservations.py` | `pitwall.cost.simulator.{WhatIfSimulator,WhatIfWorkload}`, `pitwall.finops.burn_rate.BurnRateForecast` |
| `sync_gate.py` | `pitwall.core.idempotency.reserve_idempotency_key`, `pitwall.core.models.Capability`, `pitwall.cost.{budget_gate,estimator}` |
| `threshold_alerts.py` | `pitwall.cost.notifications` (local import in `send_crossing_notifications`, `src/pitwall/cost/threshold_alerts.py:139`) |
| `alerts.py` | `pitwall.cost.notifications.{NotificationResult,Notifier,get_notifier}` (`src/pitwall/cost/alerts.py:15`) |
| `hibernate_alerts.py` | `pitwall.cost.notifications.{NotificationResult,Notifier,get_notifier}` (`src/pitwall/cost/hibernate_alerts.py:14`) |
| `exporter.py` | `pitwall.config.require_runtime_env` |
| `billing_read.py` | `pitwall.runpod_client.graphql.{RunpodCreditsBalance,RunpodGraphQLClient}`, `pitwall.cost.budget_gate.BudgetGate` (via `BudgetGateLike` protocol) |
| `reconcile_cost.py` | `asyncpg` for the optional `cost_daily` adapter; pure reconciliation uses only stdlib `datetime`, `dataclasses`, and `decimal` |
| `slo_governor.py` | `pitwall.cost.circuit_breaker.{BreakerAction,CircuitBreakerDecision}` |

**External libraries:**

| Library | Used by |
|---------|---------|
| `asyncpg` | `budget_gate`, `sync_gate`, `threshold_alerts`, `alerts`, `exporter`, `reconcile_cost` (Postgres connections/pools) |
| `httpx` | `alerts`, `hibernate_alerts` compatibility-only `http_client` type hints (`src/pitwall/cost/alerts.py:20`, `src/pitwall/cost/hibernate_alerts.py:19`) |
| `resend` | optional `email` extra; lazily imported by `ResendNotifier.send` (`pyproject.toml:58`, `src/pitwall/cost/notifications.py:94`) |
| `prometheus_client` | `exporter` (Gauge, generate_latest) |
| `fastapi` / `starlette` | `exporter` (HTTP app) |
| `uvicorn` | `exporter.main()` |
| `decimal.Decimal` | all modules handling money (stdlib) |
