"""Hermetic tests for the policy-railed Autopilot controller."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from pitwall.autopilot import (
    ActionApplyResult,
    AutopilotActionKind,
    AutopilotController,
    AutopilotHardLimits,
    AutopilotMode,
    AutopilotSignal,
)
from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability
from pitwall.cost import BudgetCircuitBreaker, WhatIfSimulator, WhatIfWorkload
from pitwall.policy import Policy, PolicyRule, PolicySet, PolicyTarget
from pitwall.providers.drift import DriftFinding, DriftSeverity
from pitwall.routing import PlanningContext, RoutingRequest

_NOW = datetime(2026, 6, 2, 14, 0, tzinfo=UTC)
_CAPABILITY_ID = "cap_autopilot"
_CAPABILITY_NAME = "embedding.autopilot"


class RecordingExecutor:
    def __init__(self) -> None:
        self.applied: list[str] = []

    def apply(self, action: Any) -> ActionApplyResult:
        self.applied.append(action.action_id)
        return ActionApplyResult(
            action_id=action.action_id,
            applied=True,
            message="recorded",
        )


class StaticSignalSource:
    def __init__(self, *signals: AutopilotSignal) -> None:
        self._signals = signals
        self.seen_now: datetime | None = None

    def collect(self, now: datetime) -> Iterable[AutopilotSignal]:
        self.seen_now = now
        return self._signals


def _capability() -> Capability:
    return Capability(
        id=_CAPABILITY_ID,
        name=_CAPABILITY_NAME,
        version="1.0.0",
        class_=CapabilityClass.EMBEDDING,
        cost_mode=CostMode.PER_SECOND,
        source=CapabilitySource.API,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider(provider_id: str = "prov_autopilot") -> dict[str, object]:
    return {
        "id": provider_id,
        "capability_id": _CAPABILITY_ID,
        "name": provider_id,
        "provider_type": ProviderType.SERVERLESS_QUEUE.value,
        "priority": 1,
        "enabled": True,
        "health_status": "healthy",
        "cold_start_p50_ms": 0,
        "recent_error_rate": 0.0,
        "config": {"cost": {"per_second_active": "0.001"}},
    }


def _simulator(
    *,
    budget_usd: Decimal = Decimal("1.000000"),
    current_spend_usd: Decimal = Decimal("0.000000"),
) -> WhatIfSimulator:
    context = PlanningContext.replay(
        now=_NOW,
        providers=[_provider()],
        capability=_capability(),
    )
    return WhatIfSimulator(
        context,
        budget_usd=budget_usd,
        current_spend_usd=current_spend_usd,
    )


def _workload() -> WhatIfWorkload:
    return WhatIfWorkload(
        request=RoutingRequest(
            capability_name=_CAPABILITY_NAME,
            capability_id=_CAPABILITY_ID,
        )
    )


def _signal(
    signal_id: str = "sig-safe",
    *,
    priority: int = 10,
    policy_allowed: bool = True,
    workloads: tuple[WhatIfWorkload, ...] | None = None,
) -> AutopilotSignal:
    return AutopilotSignal(
        signal_id=signal_id,
        source="unit-test",
        action_kind=AutopilotActionKind.SET_WARM_CAPACITY,
        target_kind="provider",
        target_id="prov_autopilot",
        reason="raise warm capacity for forecasted demand",
        priority=priority,
        confidence=Decimal("0.95"),
        params={"target_count": 2},
        policy_provider={
            "id": "prov_autopilot",
            "provider_type": ProviderType.SERVERLESS_QUEUE.value,
            "config": {"autopilot_allowed": policy_allowed},
        },
        simulation_workloads=workloads if workloads is not None else (_workload(),),
    )


def _policy_requires_autopilot_allowed() -> PolicySet:
    return PolicySet(
        policies=[
            Policy(
                id="autopilot.provider-opt-in",
                target=PolicyTarget.PROVIDER,
                rules=[
                    PolicyRule(
                        path="config.autopilot_allowed",
                        operator="equals",
                        value=True,
                        message="provider is not opted into autonomous actions",
                    )
                ],
            )
        ]
    )


def test_shadow_mode_applies_nothing_by_default() -> None:
    executor = RecordingExecutor()
    controller = AutopilotController(
        policy_set=_policy_requires_autopilot_allowed(),
        simulator=_simulator(),
        executor=executor,
    )

    result = controller.run(now=_NOW, signals=[_signal()])

    assert executor.applied == []
    assert result.mode == AutopilotMode.SHADOW
    assert result.decisions[0].outcome == "shadowed"
    assert result.applied_count == 0


def test_apply_mode_applies_policy_allowed_simulation_safe_action() -> None:
    executor = RecordingExecutor()
    controller = AutopilotController(
        policy_set=_policy_requires_autopilot_allowed(),
        simulator=_simulator(),
        executor=executor,
        mode=AutopilotMode.APPLY,
    )

    result = controller.run(now=_NOW, signals=[_signal()])

    assert executor.applied == ["ap-sig-safe"]
    assert result.decisions[0].outcome == "applied"
    assert result.applied_count == 1


def test_invalid_mode_is_rejected_before_apply_path() -> None:
    executor = RecordingExecutor()

    with pytest.raises(ValueError, match="not-a-real-mode"):
        AutopilotController(
            policy_set=_policy_requires_autopilot_allowed(),
            simulator=_simulator(),
            executor=executor,
            mode="not-a-real-mode",
        )

    assert executor.applied == []


def test_policy_denied_action_is_not_applied() -> None:
    executor = RecordingExecutor()
    controller = AutopilotController(
        policy_set=_policy_requires_autopilot_allowed(),
        simulator=_simulator(),
        executor=executor,
        mode=AutopilotMode.APPLY,
    )

    result = controller.run(now=_NOW, signals=[_signal(policy_allowed=False)])

    assert executor.applied == []
    assert result.decisions[0].outcome == "denied"
    assert result.decisions[0].gates[0].name == "policy"
    assert result.decisions[0].gates[0].passed is False


def test_action_without_simulation_workload_is_not_applied() -> None:
    executor = RecordingExecutor()
    controller = AutopilotController(
        policy_set=_policy_requires_autopilot_allowed(),
        simulator=_simulator(),
        executor=executor,
        mode=AutopilotMode.APPLY,
    )

    result = controller.run(now=_NOW, signals=[_signal(workloads=())])

    assert executor.applied == []
    assert result.decisions[0].outcome == "denied"
    assert result.decisions[0].gates[-1].name == "simulation"
    assert result.decisions[0].gates[-1].reason == "action has no simulation workload"


def test_simulation_budget_overrun_is_not_applied() -> None:
    executor = RecordingExecutor()
    controller = AutopilotController(
        policy_set=_policy_requires_autopilot_allowed(),
        simulator=_simulator(
            budget_usd=Decimal("0.050000"),
            current_spend_usd=Decimal("0.000000"),
        ),
        executor=executor,
        mode=AutopilotMode.APPLY,
    )

    result = controller.run(now=_NOW, signals=[_signal()])

    assert executor.applied == []
    assert result.decisions[0].outcome == "denied"
    assert result.decisions[0].gates[-1].name == "simulation"
    assert result.decisions[0].gates[-1].passed is False


def test_hard_limit_overrun_is_not_applied() -> None:
    executor = RecordingExecutor()
    controller = AutopilotController(
        policy_set=_policy_requires_autopilot_allowed(),
        simulator=_simulator(),
        executor=executor,
        mode=AutopilotMode.APPLY,
        limits=AutopilotHardLimits(max_reserved_usd_per_action=Decimal("0.010000")),
    )

    result = controller.run(now=_NOW, signals=[_signal()])

    assert executor.applied == []
    assert result.decisions[0].outcome == "denied"
    assert result.decisions[0].gates[-1].name == "hard_limits"
    assert result.decisions[0].gates[-1].reason == "action reserved cost exceeds hard limit"


def test_circuit_breaker_block_is_not_applied() -> None:
    executor = RecordingExecutor()
    breaker_decision = BudgetCircuitBreaker().evaluate(
        budget_usd=Decimal("1.000000"),
        mtd_spend_usd=Decimal("1.000000"),
        now=_NOW,
    )
    controller = AutopilotController(
        policy_set=_policy_requires_autopilot_allowed(),
        simulator=_simulator(),
        executor=executor,
        mode=AutopilotMode.APPLY,
    )

    result = controller.run(
        now=_NOW,
        signals=[_signal()],
        breaker_decision=breaker_decision,
    )

    assert breaker_decision.action == "block"
    assert executor.applied == []
    assert result.decisions[0].outcome == "denied"
    assert result.decisions[0].gates[-1].name == "circuit_breaker"


def test_controller_gathers_signals_from_sources() -> None:
    source = StaticSignalSource(_signal())
    controller = AutopilotController(
        policy_set=_policy_requires_autopilot_allowed(),
        simulator=_simulator(),
    )

    result = controller.run(now=_NOW, sources=[source])

    assert source.seen_now == _NOW
    assert len(result.decisions) == 1
    assert result.decisions[0].action.signal_id == "sig-safe"


def test_max_actions_per_run_limits_apply_count_deterministically() -> None:
    executor = RecordingExecutor()
    controller = AutopilotController(
        policy_set=_policy_requires_autopilot_allowed(),
        simulator=_simulator(),
        executor=executor,
        mode=AutopilotMode.APPLY,
        limits=AutopilotHardLimits(max_actions_per_run=1),
    )

    result = controller.run(
        now=_NOW,
        signals=[
            _signal("sig-b", priority=20),
            _signal("sig-a", priority=10),
        ],
    )

    assert executor.applied == ["ap-sig-a"]
    assert [decision.action.signal_id for decision in result.decisions] == [
        "sig-a",
        "sig-b",
    ]
    assert [decision.outcome for decision in result.decisions] == ["applied", "denied"]


def test_audit_output_is_deterministic_for_identical_inputs() -> None:
    controller = AutopilotController(
        policy_set=_policy_requires_autopilot_allowed(),
        simulator=_simulator(),
    )

    first = controller.run(now=_NOW, signals=[_signal()])
    second = controller.run(now=_NOW, signals=[_signal()])

    assert first.to_dict() == second.to_dict()


def test_audit_output_removes_url_userinfo_without_query() -> None:
    result = ActionApplyResult(
        action_id="ap-redaction",
        applied=True,
        message="recorded",
        details={"callback_url": "https://user:secret@example.com/path"},
    )

    assert result.to_dict()["details"] == {
        "callback_url": "https://example.com/path",
    }


def test_audit_output_removes_url_userinfo_and_redacts_query() -> None:
    result = ActionApplyResult(
        action_id="ap-redaction",
        applied=True,
        message="recorded",
        details={
            "callback_url": "https://user:secret@example.com:8443/path?token=abc#frag",
        },
    )

    audit_output = result.to_dict()

    assert audit_output["details"] == {
        "callback_url": "https://example.com:8443/path?<redacted>",
    }
    assert "user" not in str(audit_output)
    assert "secret" not in str(audit_output)
    assert "token=abc" not in str(audit_output)


def test_drift_findings_can_be_consumed_as_direct_signals() -> None:
    finding = DriftFinding(
        provider_id="prov_autopilot",
        field="availability",
        expected=True,
        observed=False,
        severity=DriftSeverity.HIGH,
        message="provider unavailable",
    )

    signal = AutopilotSignal.from_drift_finding(
        finding,
        simulation_workloads=(_workload(),),
        policy_provider={
            "id": "prov_autopilot",
            "provider_type": ProviderType.SERVERLESS_QUEUE.value,
            "config": {"autopilot_allowed": True},
        },
    )

    assert signal.action_kind == AutopilotActionKind.MARK_PROVIDER_UNHEALTHY
    assert signal.target_id == "prov_autopilot"
    assert signal.source == "drift"
