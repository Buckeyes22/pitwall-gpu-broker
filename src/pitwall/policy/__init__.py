"""Policy-as-Code schema, loading, and evaluation."""

from __future__ import annotations

from pitwall.policy.engine import evaluate_policies
from pitwall.policy.loader import (
    PolicyLoadError,
    load_default_policy_set,
    load_policy_file,
    load_policy_files,
    merge_policy_sets,
)
from pitwall.policy.schema import (
    Policy,
    PolicyCondition,
    PolicyEffect,
    PolicyEvaluationResult,
    PolicyOperator,
    PolicyRule,
    PolicySet,
    PolicyTarget,
    PolicyViolation,
)


def evaluate_default_policies(config: object) -> PolicyEvaluationResult:
    """Evaluate packaged audit-gate policies against *config*."""

    return evaluate_policies(load_default_policy_set(), config)


__all__ = [
    "Policy",
    "PolicyCondition",
    "PolicyEffect",
    "PolicyEvaluationResult",
    "PolicyLoadError",
    "PolicyOperator",
    "PolicyRule",
    "PolicySet",
    "PolicyTarget",
    "PolicyViolation",
    "evaluate_default_policies",
    "evaluate_policies",
    "load_default_policy_set",
    "load_policy_file",
    "load_policy_files",
    "merge_policy_sets",
]
