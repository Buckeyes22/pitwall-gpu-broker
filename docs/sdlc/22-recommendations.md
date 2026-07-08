# Recommendations Engine — SDLC 21

## 1. Purpose & Scope

The recommendations engine aggregates four signal planes into prioritized,
actionable operator guidance:

- **Scorecards** — cost×latency×quality metrics per (capability, provider)
- **Drift detection** — expected vs observed provider state divergence
- **Burn-rate forecasting** — budget runway and spend-trend projections
- **Reservation planning** — on-demand vs reserved/warm-pool cost evaluations

The engine is **pure, deterministic, and read-only**.  It never mutates state or
auto-applies changes.  Output is a sorted list of :class:`Recommendation`
objects ordered by priority (most urgent first).  An operator, reconciler, or
future "Broker Copilot" MCP agent consumes these recommendations and decides
whether to act.

---

## 2. Components

### `src/pitwall/recommendations/engine.py`

**Responsibility:** Convert signal snapshots into ranked recommendations.

**Key classes/functions:**

```python
@dataclass(frozen=True, slots=True)
class ScorecardMetric:
    capability_id: str
    provider_id: str
    dimension: str
    score: Decimal
    benchmark: Decimal
    message: str = ""
```

Lightweight contract for scorecard signals.  A gap of ``score`` below
``benchmark`` that exceeds the engine threshold triggers a
``switch_to_better_provider`` recommendation.

```python
class RecommendationCategory(StrEnum):
    DRIFT = "drift"
    BUDGET = "budget"
    CAPACITY = "capacity"
    SCORECARD = "scorecard"
```

High-level bucket used for filtering and UI grouping.

```python
@dataclass(frozen=True, slots=True)
class Recommendation:
    action: str
    category: RecommendationCategory
    target_provider_id: str | None
    target_capability_id: str | None
    rationale: str
    estimated_impact_usd: Decimal
    confidence: Decimal
    source_signals: tuple[str, ...]
    priority: int
```

One actionable item.  ``priority`` is an integer where lower values mean higher
urgency.  ``estimated_impact_usd`` may be zero for operational
recommendations (e.g. drift remediation) and non-zero for financial
recommendations (e.g. reservation savings).

```python
@dataclass(frozen=True, slots=True)
class RecommendationEngine:
    runway_critical_days: Decimal = Decimal("3")
    runway_warning_days: Decimal = Decimal("7")
    scorecard_threshold: Decimal = Decimal("0.15")

    def recommend(
        self,
        *,
        scorecards: Sequence[ScorecardMetric] = (),
        drift_findings: Sequence[DriftFinding] = (),
        burn_rate: BurnRateForecast | None = None,
        reservation: ReservationRecommendation | None = None,
    ) -> list[Recommendation]: ...
```

The core engine.  ``recommend`` is deterministic given the inputs and returns
results sorted by ``(priority, 1 - confidence, action)``.

**Signal → recommendation mapping**

| Signal source | Condition | Action | Priority |
|---|---|---|---|
| Drift — provider_id mismatch | Critical severity | investigate_provider_id_mismatch | 1 |
| Drift — availability | Enabled but unavailable | check_provider_availability | 5 (High) / 10 (Medium) / 15 (Low) |
| Drift — enabled | Disabled but running | disable_or_investigate_running_provider | 5 |
| Drift — enabled | Enabled but terminated | reconcile_provider_enablement | 10 |
| Drift — health_status | Any mismatch | investigate_provider_health | 5 |
| Drift — price_per_second | Any mismatch | update_provider_pricing_or_switch | 10 |
| Burn rate — runway | ≤ 3 days | reduce_spend_or_increase_budget | 2 |
| Burn rate — runway | ≤ 7 days | review_spend_trend | 6 |
| Burn rate — trend | increasing | investigate_spend_acceleration | 8 |
| Reservation — action | blocked | review_provider_pool | 3 |
| Reservation — action | reserve | reserve_capacity | 4 |
| Scorecard — any dimension | gap > 0.15 below benchmark | switch_to_better_provider | 12 |

---

### `src/pitwall/recommendations/__init__.py`

Re-exports ``Recommendation``, ``RecommendationCategory``,
``RecommendationEngine``, and ``ScorecardMetric``.  This is the package public
API.

---

## 3. Public Interfaces

### From `pitwall.recommendations`

```python
engine = RecommendationEngine(
    runway_critical_days=Decimal("3"),
    runway_warning_days=Decimal("7"),
    scorecard_threshold=Decimal("0.15"),
)

recs = engine.recommend(
    scorecards=[
        ScorecardMetric(
            capability_id="cap_llm",
            provider_id="prov_runpod_a",
            dimension="cost",
            score=Decimal("0.4"),
            benchmark=Decimal("0.8"),
            message="Per-second rate is 2× benchmark",
        )
    ],
    drift_findings=[
        DriftFinding(
            provider_id="prov_runpod_a",
            field="price_per_second",
            expected=Decimal("0.001"),
            observed=Decimal("0.002"),
            severity=DriftSeverity.MEDIUM,
        )
    ],
    burn_rate=BurnRateForecast(...),
    reservation=ReservationRecommendation(...),
)

# recs is sorted by priority ascending
for rec in recs:
    print(rec.priority, rec.action, rec.rationale)
```

---

## 4. Configuration

The engine is configured at instantiation time; there are no env vars or
settings files.

| Parameter | Default | Description |
|---|---|---|
| ``runway_critical_days`` | ``3`` | Burn-rate runway below which a critical budget recommendation is emitted |
| ``runway_warning_days`` | ``7`` | Burn-rate runway below which a warning budget recommendation is emitted |
| ``scorecard_threshold`` | ``0.15`` | Minimum gap (benchmark − score) required to emit a scorecard recommendation |

---

## 5. Failure Modes & Error Types

- **Validation errors** — ``ScorecardMetric``, ``Recommendation``, and
  ``RecommendationEngine`` validate inputs in ``__post_init__`` and raise
  ``ValueError`` on malformed data (empty ids, negative priorities,
  out-of-range confidence, etc.).
- **Determinism** — The engine contains no randomness, no I/O, and no
  time-based logic.  Identical inputs always produce identical outputs.
- **Empty signals** — Calling ``recommend()`` with no arguments returns an
  empty list.
- **Unknown drift fields** — Drift findings with fields not in the mapping
  table are silently ignored.

---

## 6. Testing

| Test file | What it covers |
|---|---|
| ``tests/unit/recommendations/test_recommendations.py`` | Hermetic unit tests for all four signal planes, validation, cross-signal integration, determinism, sorting, and ``to_dict`` roundtrips.  Includes two hypothesis property tests: scorecard-gap monotonicity and burn-rate crash-freedom. |

---

## 7. Dependencies

### Internal imports

| Module | What is used |
|---|---|
| ``pitwall.providers.drift`` | ``DriftFinding``, ``DriftSeverity`` |
| ``pitwall.finops.burn_rate`` | ``BurnRateForecast`` |
| ``pitwall.finops.reservations`` | ``ReservationRecommendation`` |

### External libraries

| Library | Used by | Purpose |
|---|---|---|
| ``hypothesis`` | tests | Property-based determinism and monotonicity tests |
