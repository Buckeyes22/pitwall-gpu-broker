"""Cost estimation and budget admission for Pitwall."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "BillingSnapshot",
    "BudgetGate",
    "BudgetGateLike",
    "BudgetRejected",
    "BudgetReconciliation",
    "BudgetRejectionReason",
    "BudgetSnapshot",
    "CostQuote",
    "CostEstimator",
    "DEFAULT_THRESHOLDS",
    "EstimatePayload",
    "GpuHourPricing",
    "PITWALL_BUDGET_GATE_LOCK_KEY",
    "PITWALL_BUDGET_LOCK_KEY",
    "PerRequestPricing",
    "PerRequestEstimator",
    "PerSecondPricing",
    "PerSecondEstimator",
    "PerTokenPricing",
    "PerTokenEstimator",
    "PerVmSecondPricing",
    "PricingModel",
    "PricingModelProtocol",
    "ProviderCost",
    "ProviderCostProjection",
    "RunPodSyncCaller",
    "SyncInferenceRejected",
    "SyncInferenceResult",
    "TaggedPricingModel",
    "ThresholdCrossing",
    "TokenUsage",
    "WhatIfBatchProjection",
    "WhatIfProjection",
    "WhatIfSimulator",
    "WhatIfWorkload",
    "estimate_cost",
    "evaluate_crossings",
    "gate_sync_inference",
    "get_estimator",
    "parse_usage_json",
    "parse_pricing_model",
    "parse_usage_sse",
    "quote_cost",
    "read_billing_snapshot",
    "reconcile_with_budget",
    "record_crossings",
    "BreakerAction",
    "BudgetCircuitBreaker",
    "CircuitBreakerDecision",
    "CircuitBreakerState",
    "ChargebackLineItem",
    "ChargebackReport",
    "SubBudget",
    "SubBudgetConfig",
    "SubBudgetGate",
    "SubBudgetRejected",
    "SubBudgetSnapshot",
    "generate_chargeback_report",
    "CostGovernor",
    "CostSLO",
    "GovernorDecision",
    "PacingAction",
    "AdjustmentDirection",
    "AsyncpgCostTruthUpRepository",
    "CostReconcileAdjustment",
    "CostReconcilePlan",
    "CostReconcileWindow",
    "CostTruthUpRepository",
    "ProviderActualCostWindow",
    "RecordedCostWindow",
    "reconcile_cost",
    "truth_up_cost_daily",
    "KillEscalationDecision",
    "KillEscalationMode",
    "KillEscalationOutcome",
    "KillEscalationPolicy",
    "KillSwitchLike",
    "evaluate_kill_escalation",
    "maybe_escalate_to_kill",
]

_BUDGET_GATE_EXPORTS = {
    "BudgetGate",
    "BudgetRejected",
    "BudgetRejectionReason",
    "BudgetSnapshot",
    "PITWALL_BUDGET_GATE_LOCK_KEY",
    "PITWALL_BUDGET_LOCK_KEY",
}

_ESTIMATOR_EXPORTS = {
    "CostQuote",
    "CostEstimator",
    "EstimatePayload",
    "GpuHourPricing",
    "PerRequestPricing",
    "PerRequestEstimator",
    "PerSecondPricing",
    "PerSecondEstimator",
    "PerTokenPricing",
    "PerTokenEstimator",
    "PerVmSecondPricing",
    "PricingModel",
    "PricingModelProtocol",
    "ProviderCost",
    "TaggedPricingModel",
    "get_estimator",
    "parse_pricing_model",
    "quote_cost",
}

_SYNC_GATE_EXPORTS = {
    "RunPodSyncCaller",
    "SyncInferenceRejected",
    "SyncInferenceResult",
    "estimate_cost",
    "gate_sync_inference",
}

_THRESHOLD_ALERTS_EXPORTS = {
    "DEFAULT_THRESHOLDS",
    "ThresholdCrossing",
    "evaluate_crossings",
    "record_crossings",
}

_USAGE_EXPORTS = {
    "TokenUsage",
    "parse_usage_json",
    "parse_usage_sse",
}

_BILLING_READ_EXPORTS = {
    "BillingSnapshot",
    "BudgetGateLike",
    "BudgetReconciliation",
    "read_billing_snapshot",
    "reconcile_with_budget",
}
_SIMULATOR_EXPORTS = {
    "ProviderCostProjection",
    "WhatIfBatchProjection",
    "WhatIfProjection",
    "WhatIfSimulator",
    "WhatIfWorkload",
}
_CIRCUIT_BREAKER_EXPORTS = {
    "BreakerAction",
    "BudgetCircuitBreaker",
    "CircuitBreakerDecision",
    "CircuitBreakerState",
}
_SLO_GOVERNOR_EXPORTS = {
    "CostGovernor",
    "CostSLO",
    "GovernorDecision",
    "PacingAction",
}
_BUDGET_KILL_ESCALATION_EXPORTS = {
    "KillEscalationDecision",
    "KillEscalationMode",
    "KillEscalationOutcome",
    "KillEscalationPolicy",
    "KillSwitchLike",
    "evaluate_kill_escalation",
    "maybe_escalate_to_kill",
}

_SUB_BUDGET_EXPORTS = {
    "ChargebackLineItem",
    "ChargebackReport",
    "SubBudget",
    "SubBudgetConfig",
    "SubBudgetGate",
    "SubBudgetRejected",
    "SubBudgetSnapshot",
    "generate_chargeback_report",
}
_RECONCILE_COST_EXPORTS = {
    "AdjustmentDirection",
    "AsyncpgCostTruthUpRepository",
    "CostReconcileAdjustment",
    "CostReconcilePlan",
    "CostReconcileWindow",
    "CostTruthUpRepository",
    "ProviderActualCostWindow",
    "RecordedCostWindow",
    "reconcile_cost",
    "truth_up_cost_daily",
}


def __getattr__(name: str) -> Any:
    if name in _BUDGET_GATE_EXPORTS:
        budget_gate = import_module("pitwall.cost.budget_gate")
        return getattr(budget_gate, name)
    if name in _ESTIMATOR_EXPORTS:
        estimator = import_module("pitwall.cost.estimator")
        return getattr(estimator, name)
    if name in _SYNC_GATE_EXPORTS:
        sync_gate = import_module("pitwall.cost.sync_gate")
        return getattr(sync_gate, name)
    if name in _THRESHOLD_ALERTS_EXPORTS:
        threshold_alerts = import_module("pitwall.cost.threshold_alerts")
        return getattr(threshold_alerts, name)
    if name in _USAGE_EXPORTS:
        usage = import_module("pitwall.cost.usage")
        return getattr(usage, name)
    if name in _BILLING_READ_EXPORTS:
        billing_read = import_module("pitwall.cost.billing_read")
        return getattr(billing_read, name)
    if name in _SIMULATOR_EXPORTS:
        simulator = import_module("pitwall.cost.simulator")
        return getattr(simulator, name)
    if name in _CIRCUIT_BREAKER_EXPORTS:
        circuit_breaker = import_module("pitwall.cost.circuit_breaker")
        return getattr(circuit_breaker, name)
    if name in _SUB_BUDGET_EXPORTS:
        sub_budgets = import_module("pitwall.cost.sub_budgets")
        return getattr(sub_budgets, name)
    if name in _SLO_GOVERNOR_EXPORTS:
        slo_governor = import_module("pitwall.cost.slo_governor")
        return getattr(slo_governor, name)
    if name in _RECONCILE_COST_EXPORTS:
        reconcile_cost = import_module("pitwall.cost.reconcile_cost")
        return getattr(reconcile_cost, name)
    if name in _BUDGET_KILL_ESCALATION_EXPORTS:
        budget_kill_escalation = import_module("pitwall.cost.budget_kill_escalation")
        return getattr(budget_kill_escalation, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
