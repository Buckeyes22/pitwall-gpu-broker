# Pitwall — Feature & Capability Roadmap

> **What this is:** a forward-looking roadmap to round out Pitwall from a *consumption broker* (create-pod / invoke-endpoint / terminate) into a full GPU control plane + multi-cloud broker + an opinionated, differentiated platform — all reachable through the CLI, REST, MCP, and a **yet-to-be-built TUI**.
> **Basis:** public provider interfaces, a source-grounded Pitwall coverage
> review, alternative GPU-provider patterns, and published broker/FinOps designs.
> **Important overlap:** the parity work below (register providers, create templates, manage volumes, seed config) is also the fix for the inert-broker / no-onboarding-path blockers — these tracks should be sequenced together.

## Contents
- [The thesis](#the-thesis)
- [Three tracks at a glance](#three-tracks-at-a-glance)
- [Part A — RunPod parity (close the CRUD gaps)](#part-a--runpod-parity)
- [Part B — Multi-cloud (the provider plugin)](#part-b--multi-cloud)
- [Part C — Novel features (the differentiation)](#part-c--novel-features)
- [Part D — The TUI (the unifying surface)](#part-d--the-tui)
- [Part E — Phased roadmap & decisions](#part-e--phased-roadmap)
- [Appendix — grounded references](#appendix)

---

## The thesis

Every tool in the landscape owns **one plane**: LLM gateways (LiteLLM/OpenRouter/Portkey) own *routing*; FinOps tools (Vantage/Kubecost) own *budget*; cluster orchestrators own *compute*. **Pitwall already owns all three** behind one I/O-free decision core — deterministic **given an explicit `now` + immutable capacity/provider snapshots** (today the planner defaults `now` to wall-clock, so the deterministic-replay substrate is a prerequisite for the simulator/time-machine features below) — fronted by a Postgres-advisory-lock budget gate that admits/denies *before* any spend (`routing/planner.py`, `cost/budget_gate.py`).

That fusion is the moat. The roadmap has three tracks, and they reinforce each other:
1. **Parity** — match `runpodctl`/RunPod MCP so Pitwall is a complete control plane, not just a consumer.
2. **Multi-cloud** — a provider plugin so the same routing/cost/audit machinery spans RunPod + Vast + Together + Lambda + …
3. **Novel** — features no competitor can copy without rebuilding around a pure, dry-runnable, budget-gated router (a what-if cost simulator, budget circuit breakers, cross-provider arbitrage, an autonomous autopilot).

The **TUI** is the surface that makes all three usable: a k9s-style operator console where every parity CRUD op, every provider, and every novel panel lives behind one keyboard-driven, read-only-by-default dashboard.

---

## Three tracks at a glance

| Track | What | Why now | Effort | Headline items |
|-------|------|---------|--------|----------------|
| **A · Parity** | Full CRUD on RunPod's 6 resources + discovery + billing | Also fixes the operability plan's onboarding blockers; table-stakes for a "broker" | **M–L** | network-volume CRUD, registry-auth CRUD, endpoint CRUD, template get/update/delete, pod start/stop/reset, GPU/DC discovery, billing read |
| **B · Multi-cloud** | `Provider` plugin + pricing-model refactor + Vast/Together/Lambda | Turns "RunPod broker" into "GPU broker"; the differentiator vs RunPod-native tools | **L** | provider interface, 6-tag pricing taxonomy, 3 first providers |
| **C · Novel** | Budget/routing/compute fusions + moonshots | The actual moat; mostly *new signals into machinery that already exists* (many S/M) | **M–L** | what-if simulator, budget circuit breaker, arbitrage scoring, semantic cache, autopilot |
| **TUI** | Textual operator console unifying all of the above | The "yet-to-be-built TUI" the whole effort routes through | **M** (shell) + incremental | per-resource views + cost/routing/policy panels |

---

## Part A — RunPod parity

**Goal:** anything you can do with `runpodctl` or the RunPod MCP, Pitwall can do — exposed through CLI + REST + MCP + TUI. RunPod's surface decomposes into **6 REST resources** + **4 specialized backends** (GraphQL for spot-bids/savings/live-pricing, the per-endpoint Job API, the S3 volume API, and runpodctl-only utilities). Pitwall is deliberately a *consumption broker* today, so most management CRUD is missing.

### A.1 Coverage gap matrix
Current coverage from the audit (`src/pitwall/runpod_client/*`); target = full CRUD reachable on all surfaces.

| RunPod resource | Pitwall today | Target ops to add | Backend needed | Effort | TUI view |
|-----------------|---------------|-------------------|----------------|--------|----------|
| **Pods** | PARTIAL — create/get/list/delete (`pods.py:620,1206,1227,1274`) | **start / stop / reset / restart / update** (`POST /pods/{id}/{start,stop,reset,restart}`, `PATCH /pods/{id}`) | REST | S–M | Pods/Leases |
| **Serverless endpoints** | PARTIAL — invoke (queue+LB) FULL; only `hibernate` admin (`endpoints.py:13`) | **create / list / get / update / delete** endpoint (`POST/GET/PATCH/DELETE /endpoints`), full scaling config (workersMin/Max, idleTimeout, scalerType, flashboot, executionTimeoutMs) | REST | M | Endpoints |
| **Templates** | PARTIAL — create+cache, list via GraphQL (`templates.py:187,108`) | **get / update / delete** (`/templates/{id}`), search, Hub-deploy | REST (+runpodctl Hub) | S | Templates |
| **Network volumes** | **NONE** (attach-by-ID only, `pods.py:606`) | **create / list / get / resize / delete** (`/networkvolumes`) **+ S3 file access** (`s3api-{dc}.runpod.io`, separate S3 keys) | REST + S3 API | M | Volumes |
| **Container registry auth** | **NONE (create)** — selects existing ID by env (`registry.py:57`) | **create / list / get / delete** (`/containerregistryauth`) — note: no update, delete+recreate | REST | S | Registry |
| **GPU types / availability** | PARTIAL — hardcoded name list + dead cache (`gpu.py:15-37`, `availability.py:49`) | **live GPU list + price + bid/availability** | **GraphQL** (`gpuTypes{lowestPrice,minimumBidPrice}` — REST has no list) | M | Catalog |
| **Datacenters** | NONE (env passthrough) | **list datacenters + availability** | runpodctl/GraphQL | S | Catalog |
| **Billing / credits** | NONE — cost is *estimated* locally (`cost/estimator.py`) | **read actuals** (`GET /billing/{pods,endpoints,networkvolumes}`) + balance (`myself{currentBalance}`) | REST + GraphQL | M | Cost |
| **Spot / interruptible bids** | NONE | **bid pricing + bid-resume** (`podRentInterruptable(bidPerGpu)`, `podBidResume`) | **GraphQL ONLY** | M | Pods/Market |
| **Savings plans** | NONE | read + purchase | GraphQL/console | M | Cost |
| **SSH keys / secrets** | NONE (SSH only probed) | manage SSH keys, `RUNPOD_SECRET_*` | runpodctl/console | S | Settings |
| **Pod logs / exec** | NONE (HTTP/SSH probes only) | log fetch, remote exec | (proxy/SSH) | M | Pods |
| **File transfer** | NONE | volume up/down via S3; (croc send/receive optional) | S3 API | S–M | Volumes |

### A.2 Integration implications (don't miss these)
- **Spot/bid pricing and live GPU prices are GraphQL-only** — REST `interruptible:true` creates spot but can't *set or read a bid*. Any cost-arbitrage feature (Part C) needs a GraphQL client for `gpuTypes`/`podRentInterruptable`. Pitwall already uses GraphQL for one read (`templates.py:108`) — extend it.
- **Running serverless work is the per-endpoint Job API** (`api.runpod.ai/v2/{id}`), which Pitwall *already* wraps fully (`queue.py`) — good. But endpoint *management* (create/scale/delete) is the management REST API Pitwall lacks.
- **Network-volume data access is the S3 API** (separate S3 keys, datacenter-specific endpoints) — the only way to read/write volume files without booting a pod. This replaces the current "boot a pod and run a script" warm-volume hack (`cli.py:564`).
- **Container-registry-auth has no update anywhere** — model it as delete+recreate.

### A.3 Parity also fixes onboarding
Adding endpoint-create, network-volume-create, registry-auth-create, GPU/DC discovery, and a `pitwall-gpu-broker init`/seed directly resolves the operability plan's **inert-broker blockers** (A-B1…A-B3 there). Build them once. A `pitwall-gpu-broker init` wizard that discovers GPUs/DCs, creates a template + registry-auth, registers a first endpoint as a provider, and seeds a capability is the single highest-leverage parity deliverable.

---

## Part B — Multi-cloud

**Goal:** the same routing/cost/audit/lease machinery spans RunPod **and** other GPU clouds behind a `Provider` plugin. Today everything is RunPod-shaped.

### B.1 The central finding — two cost-model worlds
Candidate providers split into:
- **Per-second** (RunPod-like — estimator reuses existing math): **Vast.ai**, **Lambda**, **TensorDock**, **CoreWeave**, **Crusoe**, **Fly**, **DataCrunch**, **Hyperstack**, **Modal**, **Baseten-dedicated**.
- **Per-token / per-run** (NO RunPod analog — *breaks* the per-second estimator + budget gate): **Together** (per-M-tokens, in/out split), **Replicate** (per-output or per-second), **Fal** (per-MP / per-sec-output).

Supporting multi-cloud therefore requires refactoring the cost estimator into a **tagged pricing model**, which is the real work (the transport is easy).

### B.2 The `Provider` plugin interface (design sketch)
```python
class Provider(Protocol):
    def id(self) -> str
    def surfaces(self) -> set[Surface]                 # {RAW_POD, SERVERLESS_ENDPOINT, SERVERLESS_INFERENCE}
    def credential_schema(self) -> CredSpec            # bearer | token-pair (Modal) | header (Hyperstack) | kubeconfig (CoreWeave) | OAuth (DataCrunch)
    def list_capabilities(self) -> list[Capability]    # models OR gpu-types+regions
    def get_availability(self, req) -> CapacitySignal  # marketplace offers / instance stock / queue depth / always-on
    def estimate_cost(self, req) -> CostEstimate       # PricingModel-tagged (B.3), NOT a float
    def launch(self, req) -> Handle                    # provision pod/endpoint OR submit inference
    def route(self, ref) -> Result | JobRef            # sync (Together/Vast) vs async (Replicate/Fal/Modal)
    def get_status(self, ref) -> Status                # +PREEMPTED for spot
    def get_result(self, ref) -> Result
    def teardown(self, handle) -> None                 # delete pod | scale-to-zero | no-op (shared inference)
    def health(self) -> Health
    def reconcile_cost(self, ref) -> ActualCost        # provider-reported actuals → budget truth-up
```
**Where providers diverge (must be first-class):** cost model (tagged union), sync-vs-async, credential shape, capacity signal, teardown semantics (real / scale-to-zero / no-op), and **preemption** (spot can be killed → new `PREEMPTED` status + audit risk flag).

### B.3 Pricing-model taxonomy the estimator/gate must support
`PER_SECOND_ACTIVE` · `PER_SECOND_ACTIVE_WITH_IDLE` (standing endpoint cost: Replicate/Baseten/Together-dedicated) · `PER_MILLION_TOKENS_SPLIT` (in/out separate; gate must enforce `max_tokens` since output is the unbounded cost) · `PER_RUN_UNIT` (image/MP/video-sec) · `PER_REQUEST_FLAT` · `SUBSCRIPTION_RESERVED`. Plus a **bid/spot overlay** (estimate is a *ceiling at the bid* + preemption-probability flag). `estimate_cost` returns `{model, components, est_total, ceiling, confidence}`; the budget gate branches on `model` to pick the cap (max-seconds / max-tokens / max-units / max-requests); `reconcile_cost` truth-ups with provider actuals.

### B.4 Prioritized providers
| Wave | Providers | Why | Effort |
|------|-----------|-----|--------|
| **1 (first sprint)** | **Vast.ai** + **Together** + **Lambda** | Covers all 3 pricing worlds in one sprint: Vast = cheaper per-second raw-pod + a bid axis RunPod lacks; Together = first per-token capability (proves the tagged cost model); Lambda = premium on-demand VM tier | ~1 week total |
| **2** | Replicate, Fal, TensorDock, DataCrunch, Hyperstack, Baseten | More inference + more per-second arbitrage sources; each ~2 days once the adapter pattern exists | M |
| **Aspirational** | Modal (SDK-first), CoreWeave (K8s), Crusoe/Fly (niche), AWS/GCP/Azure (heavyweight IAM) | Don't fit the clean REST mold; defer | L |
| **Avoid near-term** | Paperspace/DO | Gradient API deprecated 2024-07-15, replacement in flux | — |

Doing Wave 1 forces exactly the abstractions the broker needs (tagged cost, async routing, varied credentials, preemption) rather than RunPod-plus-one.

---

## Part C — Novel features

The differentiation track. The recurring pattern: most ideas are **new signals/score-terms plugged into machinery that already exists** (the scorer already reads `cost_per_second_active`, `cold_start_p50_ms`, `warm_workers`, `recent_error_rate`, `priority_multiplier`; the gate already admits/denies under an advisory lock) — which is why so many are S/M, not L.

### C.1 Budget / FinOps
| # | Feature | One-liner | TUI panel | Effort |
|---|---------|-----------|-----------|--------|
| 1.1 | **Burn-Rate Forecaster** | Project spend to month-end; expose `budget.eta_to_cap` + forecast-overshoot — a *forecasted breach date* tied to a hard gate (nobody surveyed does this) | "Budget Runway" fuel-gauge w/ projected-empty timestamp | S |
| 1.2 | **Budget Circuit Breaker** | At 80/95% of cap, don't 500 — *auto-downgrade* the capability to a cheaper tier; a financial circuit breaker (cascades escalate on *quality*; nobody escalates *down* on *budget*) | "Degradation Ladder" w/ next-step $ saved | M |
| 1.3 | **Blast-Radius Sub-Budgets** | Hierarchical org→team→key pre-spend admission w/ isolation + chargeback (beyond LiteLLM flat caps); one runaway agent can't drain the org | spend treemap by org/team/key + chargeback export | M |
| 1.4 | **What-If Cost Simulator** ⭐ | Replay traces through the I/O-free planner with an explicit `now` + a captured capacity/provider snapshot (prerequisite: the deterministic-replay substrate; today `now` defaults to wall-clock) and a modified config → exact counterfactual monthly bill, zero paid calls. **The cost estimator is Decimal-exact; once replay determinism lands this is a structural moat.** | "Simulator" tab: config-diff → projected bill delta | M |
| 1.5 | **Cost SLO + Spend-Velocity Governor** | A token bucket that refills in *dollars* not requests; throttle (not deny) when spend-rate exceeds a cost SLO — marries FinOps to SRE | "Spend Velocity" speedometer + error-budget bar | S–M |
| 1.6 | **Reservation/Warm-Pool Recommender** | "Reserve 2 warm A100s → save $412/mo, break-even at 61% (you're at 78%)" from lease histograms + Decimal cost model | "Commitment Advisor" card | M |

### C.2 Compute / orchestration
| # | Feature | One-liner | TUI panel | Effort |
|---|---------|-----------|-----------|--------|
| 2.1 | **Price×Latency Arbitrage Scoring** ⭐ | A tunable arbitrage weight λ so each request picks its point on the cost/latency Pareto frontier — per-request, deterministic given fixed inputs (unlike provision-time, job-granular schedulers) | cost-vs-latency scatter w/ Pareto frontier + λ slider | S |
| 2.2 | **Hedged Multi-Provider Racing** | Fire latency-critical requests at top-2 providers, take first valid, cancel + refund loser; budget reserves worst-case up front | live "race" strip per request | M |
| 2.3 | **Demand-Forecast Prewarm** | Forecast per-capability demand (seasonality) and pre-scale `warm_workers` ahead of the curve via the reconciler — closed loop with the score signal | 24h demand-vs-warm-pool overlay | M |
| 2.4 | **Carbon-Aware Scheduling** | `carbon_intensity` per region (Electricity Maps, cached) as a soft score term for deferrable work → a cost/latency/**carbon** tri-objective router | "Grid" tiles colored by live carbon + kg-CO₂-saved | M |
| 2.5 | **Spot↔On-Demand Failover** | Checkpoint a lease on preemption, relaunch on another pool; budget-aware spot/on-demand arbitrage for broker leases | lease timeline stitching spot/on-demand segments | L |
| 2.6 | **Request Coalescing** | A short broker-side window collapses identical concurrent requests into one upstream call, charged once (extends idempotency keys) | "calls saved today / $X" counter | S–M |

### C.3 Routing / inference intelligence
| # | Feature | One-liner | TUI panel | Effort |
|---|---------|-----------|-----------|--------|
| 3.1 | **Budget-Aware Semantic Cache** ⭐ | Embedding-similarity cache *in front of* the budget gate; a hit short-circuits before spend and protects the cap; org-shared (GPTCache/Portkey are per-app) | "Cache" hit-rate dial + "$ protected from cap" | M |
| 3.2 | **Complexity-Based Cascade Routing** | Cheap model first; escalate to a stronger capability on low confidence — cascade routing (45–85% cost cut) as a *first-class broker capability* sharing one gate + fallback engine | "Cascade" sankey w/ % escalated per tier | M–L |
| 3.3 | **Quality-Aware Routing** | Offline LLM-judge over sampled traces → per-(capability,provider) win-rate as a `quality_score` term; continuous measured quality as a *routing* input | per-capability scorecard w/ win-rate CIs | M |
| 3.4 | **Shadow / Canary + Auto-Promote** | Mark a provider `shadow`/`canary X%`; auto-promote on win-rate+error thresholds, auto-rollback otherwise | "Rollout" board (Argo-Rollouts-style) | M |
| 3.5 | **Org-Wide Prompt/Embedding Dedup** | Content-hash exact-match dedup before the gate, single-charge (tenant-scoped) | "dedup saved N calls / $X" tile | S |

### C.4 Reliability / governance
| # | Feature | One-liner | TUI panel | Effort |
|---|---------|-----------|-----------|--------|
| 4.1 | **Policy-as-Code** (spend+routing+safety) ⭐ | One OPA/Rego-style engine evaluated at the pre-spend audit gate — unifies spend policy + routing policy + safety policy that competitors split across 3 tools | "Policies" list + deny-counts + simulator | L |
| 4.2 | **GitOps for Capabilities/Providers** | Capabilities/providers/budgets/policies as versioned YAML reconciled into the DB; PR = review, revert = rollback | "Config Drift" declared-vs-live diff | M |
| 4.3 | **Automated Chaos / Kill-Drills** | Scheduled fallback + kill-switch drills (slice-isolated, dry-run-first); asserts fallback fires within SLA | "Resilience" panel + MTTR-to-fallback | M |
| 4.4 | **Pre-Spend Guardrails** | PII/secret scanning on prompts as audit checks 17+ — provider-agnostic, enforced *before* spend across all downstream providers | "Guardrails" blocked/redacted feed | M |

### C.5 Observability / intelligence
| # | Feature | One-liner | TUI panel | Effort |
|---|---------|-----------|-----------|--------|
| 5.1 | **Recommendations Engine** | Replays traces through the simulator trying config perturbations, ranks by $ saved per quality/latency lost → "switch X to Y, save $312/mo, +40ms" with one-click GitOps apply | "Recommendations" inbox | M |
| 5.2 | **Cost×Latency×Quality Scorecards** | One pane fusing the three planes per (capability, provider); radar chart; "this provider is Pareto-dominated — retire it?" | sortable scorecard grid + radar | S–M |
| 5.3 | **Provider Drift Detection** | Change-point detection on each provider's latency/error/win-rate → auto-raise cooldown or re-canary | "Drift" timelines w/ change-point markers | M |
| 5.4 | **Broker Copilot** | An MCP agent that reads scorecards/recs/drift and *proposes* config changes as GitOps PRs (never auto-applies) | "Copilot" chat dock + proposed-changes tray | M |

### C.6 Moonshots
| # | Feature | One-liner | Effort |
|---|---------|-----------|--------|
| 6.1 | **Autonomous Autopilot** ⭐ | A closed loop (bandit/Bayesian) that continuously co-optimizes routing weights + warm-pool sizes + budget allocations against declared SLOs, *within* policy rails, every action dry-run-validated and kill-switchable. Feasible **only** because every Pitwall action is already dry-runnable + budget-gated + kill-switchable (and deterministically replayable once the deterministic-replay substrate is in place) — that safety substrate is why Pitwall can attempt this when generic tools can't. | L |
| 6.2 | **Real-Time Cross-Provider Spot Market** | Per-lease live re-bidding across providers on a price×latency×carbon frontier, with checkpoint-migrate (2.5) when a cheaper bid appears + hysteresis to avoid thrash | L |
| 6.3 | **Counterfactual "Time-Machine"** | Productize 1.4: versioned trace archive + pure planner = exact, auditable "what would Q1 have cost under contract B?" for audits/negotiations | M–L |

### C.7 Top 7 bets & the killer synergy
Ranked by (value × differentiation ÷ effort): **1.2** Budget Circuit Breaker · **1.4** What-If Simulator · **2.1** Arbitrage Scoring · **3.1** Budget-Aware Semantic Cache · **1.3** Blast-Radius Sub-Budgets · **4.1** Policy-as-Code · **6.1** Autopilot.

**Build 1.4 (the simulator) first — it's the load-bearing primitive under half the roadmap:** it makes recommendations (5.1) trustworthy, lets policies (4.1) be tested before they brick prod, and gives autopilot (6.1) a dry-run sandbox. The second cluster is the **tri-objective router** (2.1 arbitrage + 2.4 carbon + 3.3 quality), turning the scorer from cost/latency into a cost×latency×quality×carbon optimizer no surveyed competitor can claim.

---

## Part D — The TUI

The "yet-to-be-built TUI" is the surface that makes parity + multi-cloud + novel usable. Per the TUI research: **Textual** (modern, async, testable, built on Rich), **k9s-style** navigation, **read-only by default** with type-to-confirm for destructive ops, a persistent footer showing active keys, and it must **share Pitwall's service layer** with the CLI (never duplicate logic).

### D.1 Information architecture (views)
A k9s-style multi-view console; `:` command bar to jump views, `/` to filter, `?` help, `q` quit, `r` refresh.

| View | Surfaces (parity + novel) | Read-only / actionable |
|------|---------------------------|------------------------|
| **Overview** | fleet health: active leases, Budget Runway (1.1), provider health, recent kill-log, audit status, spend velocity (1.5) | read-only |
| **Providers** | list/register/enable/disable/hibernate; health; **cost×latency×quality scorecards (5.2)**; drift markers (5.3); register-from-RunPod **and** other clouds (Part B) | actionable (type-to-confirm) |
| **Capabilities** | list/create/update; **cascade config (3.2)**; routing weights; quality scorecards | actionable |
| **Pods / Leases** | live leases; launch/renew/stop/teardown; **start/stop/reset (A.1)**; **spot↔on-demand timeline (2.5)**; logs/exec | actionable, danger-styled teardown |
| **Endpoints** | **serverless CRUD + scaling config (A.1)**; hibernate; scaler type/idle/flashboot | actionable |
| **Templates** | **CRUD + Hub browse/deploy (A.1)** | actionable |
| **Volumes** | **network-volume CRUD + S3 file browser (A.1)** | actionable, danger-styled delete |
| **Registry** | **container-registry-auth CRUD (A.1)** | actionable |
| **Catalog** | **live GPU types + price + bid + availability (GraphQL), datacenters (A.1)** | read-only |
| **Cost** | Budget Runway (1.1), **sub-budget treemap + chargeback (1.3)**, **what-if Simulator (1.4)**, **Recommendations inbox (5.1)**, billing actuals, reservation advisor (1.6) | read-only + "apply via PR" |
| **Routing** | **arbitrage Pareto frontier + λ (2.1)**, **cascade sankey (3.2)**, **cache hit-rate (3.1)**, hedge races (2.2), carbon grid (2.4) | read-only + tuning |
| **Policies** | **policy-as-code list + simulate (4.1)**, config drift (4.2), guardrails feed (4.4) | actionable via PR |
| **Jobs** | job status/stream/cancel (existing Job API) | actionable |
| **Resilience** | chaos/kill-drills (4.3), fallback-coverage matrix | actionable (dry-run-first) |
| **Autopilot** | SLO dials, actions log, big-red disengage (6.1) | guarded |

### D.2 Build approach
- **Phase 1 (now, cheap):** adopt **Rich** for CLI output (audit table, cost report, lease list) — auto TTY/`NO_COLOR` handling, no new framework — and harden the `click` CLI contract (`--json`, exit codes, `--dry-run`/type-to-confirm on destructive ops). Most UX win, stays scriptable.
- **Phase 2:** a **Textual** `pitwall-gpu-broker dashboard` shell with the **Overview + Providers + Leases + Cost** views (read-only) over the existing service layer; `Pilot` headless tests + `pytest-textual-snapshot` folded into the coverage ratchet.
- **Phase 3+:** add views as their backing features land (Endpoints/Templates/Volumes as parity ships; Routing/Policies/Autopilot as novel ships). The TUI grows with the roadmap rather than blocking on it.

---

## Part E — Phased roadmap

Sequencing across all three tracks. Effort: S ≤ 0.5d · M 0.5–2d · L > 2d (per feature; phases are multi-week).

### Phase 1 — Parity core + onboarding *(unblocks the operability plan too)*
- A · network-volume CRUD + S3 access · registry-auth CRUD · endpoint CRUD + scaling config · template get/update/delete · pod start/stop/reset · GPU/DC discovery (GraphQL) · billing read.
- A · **`pitwall-gpu-broker init` wizard + seed** (discovers GPUs/DCs → creates template + registry-auth → registers first provider → seeds a capability → reaches a working inference). *Directly closes operability blockers A-B1…A-B3.*
- TUI · Rich output + `click` contract hardening (D.2 Phase 1).
- **Why first:** it's table-stakes "broker" completeness *and* the onboarding fix; everything else assumes a user can stand up providers.

### Phase 2 — The TUI shell
- Textual `pitwall-gpu-broker dashboard`: Overview + Providers + Leases + Cost views, read-only, over the existing service layer (D.2 Phase 2). Headless + snapshot tests.
- **Why second:** gives the parity CRUD a human surface and a foundation every later feature plugs a panel into.

### Phase 3 — The cost-model refactor + first multi-cloud
- B · refactor estimator/budget gate to the **tagged pricing model** (B.3) — the prerequisite for both per-token providers *and* several novel features.
- B · the `Provider` plugin interface (B.2) + **Vast + Together + Lambda** (Wave 1).
- C · **1.4 What-If Simulator** (the load-bearing primitive) — feasible now that traces + pure planner exist.
- **Why here:** the pricing refactor is a fork in the road; doing it before piling on novel budget features avoids rework.

### Phase 4 — Novel quick wins (high value ÷ effort)
- C · 2.1 arbitrage scoring (S) · 1.1 burn-rate forecaster (S) · 1.2 budget circuit breaker (M) · 3.1 budget-aware semantic cache (M) · 1.3 blast-radius sub-budgets (M) · 5.2 unified scorecards (S–M) · 2.6 coalescing (S–M).
- TUI · Cost + Routing views light up as these land.
- **Why here:** each is mostly a new signal into existing machinery; together they deliver the "owns all three planes" story.

### Phase 5 — Platform & moonshots
- C · 4.1 policy-as-code · 4.2 GitOps · 5.1 recommendations engine · 3.2 cascade · 3.3 quality routing · 3.4 shadow/canary · 2.3 prewarm · 2.4 carbon · 2.5 spot-failover.
- C · **6.1 Autopilot** (consumes 1.1/1.2/1.3/2.1/2.3/3.3/4.1/5.1) · 6.2 spot market · 6.3 time-machine.
- B · provider Wave 2 (Replicate/Fal/TensorDock/…).
- **Why last:** these compound on the simulator, the tagged cost model, and the sub-budgets built earlier.

### Decisions for the maintainer
| # | Decision | Note |
|---|----------|------|
| F1 | **How far to chase parity?** Full RunPod control plane vs "consumption + the management ops the TUI needs." | Recommend: full CRUD on the 6 resources (it's bounded and unblocks onboarding); defer SSH-keys/savings-plans/Hub-publishing. |
| F2 | **Multi-cloud now or later?** | Recommend: do the **tagged cost-model refactor** early (Phase 3) even if you only ship RunPod — it's the fork that's expensive to retrofit; add Vast/Together once the abstraction exists. |
| F3 | **Which novel bets define the product?** | Recommend the simulator (1.4) + tri-objective router (2.1+2.4+3.3) + budget circuit breaker (1.2) as the identity; autopilot (6.1) as the north star. |
| F4 | **GraphQL dependency.** Spot bids, savings plans, and live GPU pricing are GraphQL-only — commit to a GraphQL client or forgo spot-arbitrage features. | Affects 2.1/2.5/6.2. |
| F5 | **TUI scope for v1.** Read-only monitor first, or actionable from day one? | Recommend read-only Overview/Cost first (safe, high value), add actionable views behind type-to-confirm as parity lands. |

---

## Appendix

**RunPod surface (parity targets):** REST `rest.runpod.io/v1` (Pods/Endpoints/Templates/NetworkVolumes/ContainerRegistryAuths/Billing, full CRUD + pod start/stop/reset/restart); GraphQL `api.runpod.io/graphql` (spot bids `podRentInterruptable`/`podBidResume`, savings plans, `gpuTypes{lowestPrice,minimumBidPrice}`, `cpuTypes`); per-endpoint Job API `api.runpod.ai/v2/{id}` (run/runsync/status/stream/cancel/retry/health/purge-queue); S3 API `s3api-{dc}.runpod.io` (volume files, separate keys); `runpodctl` (croc send/receive, ssh keys, doctor, Hub). RunPod MCP = REST CRUD on {pod,endpoint,template,network-volume,registry-auth} + start/stop pod (reduced param set; no bids/Job-API/billing/registry-update).

**Pitwall current coverage (audit):** `runpod_client/` — pods create/get/list/delete (`pods.py:620,1206,1227,1274`), queue Job API FULL (`queue.py:201-278`), LB/serverless invoke (`lb.py`, `serverless_lb.py`), templates create+list (`templates.py:187,108`), endpoint hibernate-only (`endpoints.py:13`), GPU local-validate (`gpu.py:114`), registry select-by-env (`registry.py:57`); network-volumes/datacenters/billing/registry-create/endpoint-CRUD/pod-lifecycle = NONE. Value-add layer to preserve: routing (`routing/*`, `resolver/service.py`), cost+budget gate (`cost/estimator.py`, `cost/budget_gate.py`), 16-check audit (`audit/sixteen_check.py`), leases+TTL (`leases/state.py`, `api/leases/*`), webhooks, reconciler, kill-switch, rate limits, idempotency.

**Provider research:** per-second cohort (Vast/Lambda/TensorDock/CoreWeave/Crusoe/Fly/DataCrunch/Hyperstack/Modal/Baseten) vs per-token/run cohort (Together/Replicate/Fal); Wave-1 = Vast + Together + Lambda. Pricing taxonomy: PER_SECOND_ACTIVE · …_WITH_IDLE · PER_MILLION_TOKENS_SPLIT · PER_RUN_UNIT · PER_REQUEST_FLAT · SUBSCRIPTION_RESERVED (+ bid/spot overlay).

**State-of-the-art surveyed (novel track):** LLM gateways (LiteLLM/OpenRouter/Portkey/Cloudflare AI Gateway), semantic caching (GPTCache), cascade routing (RouteLLM, 45–85% cost cut), managed spot + checkpointing schedulers, FinOps anomaly detection (FinOps Foundation), carbon-aware scheduling (Electricity Maps), speculative decoding (vLLM), OPA/Rego policy-as-code, OWASP LLM Top 10. Pitwall's differentiation: it already owns routing **+** budget **+** compute behind one I/O-free, dry-runnable, advisory-lock-gated core (deterministic once the deterministic-replay substrate is in place) — the novel features are *fusions across planes* competitors structurally can't copy.

---

*Drafted 2026-05-31 from public provider surfaces and the repository state at
that date. Re-verify every parity claim against current source and current
provider documentation before implementation.*
