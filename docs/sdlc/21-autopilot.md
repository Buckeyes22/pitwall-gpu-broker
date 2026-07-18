# Autonomous Autopilot

## 1. Purpose & Scope

`pitwall.autopilot` is the closed-loop controller for autonomous, policy-railed
actions. It gathers recommendation, scorecard, and drift signals; proposes
deterministic actions; gates those actions through Policy-as-Code; runs the
What-If simulator as a pre-flight check; enforces hard limits and circuit-breaker
state; and applies only when explicitly configured for apply mode.

Default mode is `shadow`. In shadow mode the controller records the same audit
trail it would use for apply mode, but it never calls the executor.

The controller is deliberately injected and hermetic:

- no provider calls
- no database writes
- no wall-clock reads after the caller supplies `now`
- no mutation path except the optional executor in `AutopilotMode.APPLY`

## 2. Components

### `autopilot/schema.py` — DTOs and audit records

Key types:

```python
class AutopilotMode(StrEnum):
    SHADOW = "shadow"
    APPLY = "apply"

class AutopilotActionKind(StrEnum):
    SET_WARM_CAPACITY = "set_warm_capacity"
    MARK_PROVIDER_UNHEALTHY = "mark_provider_unhealthy"
    RESERVE_CAPACITY = "reserve_capacity"
    ADJUST_PROVIDER_PRIORITY = "adjust_provider_priority"

@dataclass(frozen=True, slots=True)
class AutopilotSignal:
    signal_id: str
    source: str
    action_kind: AutopilotActionKind
    target_kind: str
    target_id: str
    reason: str
    priority: int
    confidence: Decimal
    params: Mapping[str, object]
    policy_provider: Mapping[str, object] | None
    policy_workloads: tuple[Mapping[str, object], ...]
    policy_capability: Mapping[str, object] | None
    simulation_workloads: tuple[WhatIfWorkload, ...]
```

`AutopilotSignal.from_prewarm_recommendation(...)` adapts routing prewarm output
into `SET_WARM_CAPACITY`. `AutopilotSignal.from_drift_finding(...)` adapts direct
provider drift findings into provider-health actions. Scorecard or future
recommendation packages should implement `AutopilotSignalSource` and return the
same normalized signal shape.

`AutopilotDecision` records one action, all gates that ran, the simulation result
when present, and the executor result when apply mode actually mutates state.
`AutopilotRunResult.to_dict()` is deterministic and JSON-safe. Audit serialization
redacts sensitive field names and strips query strings/fragments from HTTP URLs.

### `autopilot/controller.py` — Control loop

Entry point:

```python
controller = AutopilotController(
    policy_set=policy_set,
    simulator=what_if_simulator,
    executor=executor,
    mode=AutopilotMode.SHADOW,
    limits=AutopilotHardLimits(),
)

result = controller.run(
    now=now,
    signals=signals,
    sources=sources,
    breaker_decision=breaker_decision,
)
```

The controller sorts all gathered signals by `(priority, source, signal_id,
target_kind, target_id)`, then derives action ids as `ap-{signal_id}`. Identical
inputs produce identical action ordering and audit output.

## 3. Gate Order

For each action:

1. **Policy** — builds a policy snapshot from the action's provider, workload,
   and capability snapshots and calls `evaluate_policies(...)`.
2. **Simulation** — requires at least one `WhatIfWorkload` and calls
   `WhatIfSimulator.simulate_workloads(...)`.
3. **Hard limits** — checks action count, per-action reserved cost, run reserved
   cost, and projected spend ceilings.
4. **Circuit breaker** — blocks apply when the supplied breaker decision is
   `downgrade` or `block`.
5. **Mode / executor** — shadow mode records `shadowed`; apply mode requires an
   executor and records the executor result.

Policy-denied actions are not simulated or applied. Actions without simulation
workloads, simulations that raise, and simulations that would exceed budget are
denied before hard-limit or executor gates.

## 4. Hard Limits

`AutopilotHardLimits` is the final controller-local stop before breaker and
executor gates:

```python
@dataclass(frozen=True, slots=True)
class AutopilotHardLimits:
    max_actions_per_run: int = 5
    max_reserved_usd_per_action: Decimal | None = None
    max_reserved_usd_per_run: Decimal | None = None
    max_projected_spend_usd: Decimal | None = None
```

All money is `Decimal` quantized to six USD places. A limit of `None` means the
controller does not enforce that specific ceiling. `max_actions_per_run=0` is a
valid fail-closed configuration.

## 5. Apply Contract

Autopilot has no built-in provider mutation. Mutations must be implemented by an
injected executor:

```python
class AutopilotExecutor(Protocol):
    def apply(self, action: AutopilotAction) -> ActionApplyResult: ...
```

The controller calls `apply(...)` only when all gates pass and
`mode=AutopilotMode.APPLY`. Without an executor, apply mode denies the action and
records `apply mode requires an executor`.

## 6. Invariants

- Shadow mode never calls the executor.
- Policy-denied actions are never applied.
- Actions without a simulator pre-flight are never applied.
- Simulator budget overruns are never applied.
- Circuit-breaker `downgrade` and `block` decisions prevent apply.
- The audit trail contains one `AutopilotDecision` per proposed action.
- Control-loop output is deterministic for identical `now`, signals, sources,
  policy set, simulator context, limits, and breaker decision.

## 7. Testing

Hermetic coverage lives in:

- `tests/autopilot/test_controller.py`
- `tests/property/test_autopilot_properties.py`

The unit suite covers shadow default, explicit apply mode, policy denial,
missing simulation, simulator budget overrun, hard-limit denial, circuit-breaker
blocking, source collection, deterministic apply ordering, deterministic audit
serialization, and drift findings as direct signals.

The property test checks the core safety invariant that shadow mode never applies
actions across generated signal sets.
