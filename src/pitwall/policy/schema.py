"""Pydantic schema for Pitwall Policy-as-Code documents."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field


class PolicyTarget(StrEnum):
    """Configuration target kind evaluated by a policy."""

    CAPABILITY = "capability"
    PROVIDER = "provider"
    WORKLOAD = "workload"


class PolicyOperator(StrEnum):
    """Supported deterministic policy comparison operators."""

    EXISTS = "exists"
    NOT_EXISTS = "not_exists"
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    CONTAINS_ALL = "contains_all"
    CONTAINS_NONE = "contains_none"
    GTE = "gte"
    LTE = "lte"
    MATCHES = "matches"


class PolicyEffect(StrEnum):
    """Policy effects supported at the audit gate."""

    DENY = "deny"


class _PolicyModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        use_enum_values=False,
    )


class PolicyCondition(_PolicyModel):
    """A single predicate over a dotted configuration path."""

    path: str = Field(min_length=1)
    operator: PolicyOperator
    value: Any = None


class PolicyRule(PolicyCondition):
    """A required predicate that records a violation when it fails."""

    message: str | None = None


class Policy(_PolicyModel):
    """One declarative policy evaluated against one target kind."""

    id: str = Field(min_length=1)
    target: PolicyTarget
    description: str = ""
    effect: PolicyEffect = PolicyEffect.DENY
    when: tuple[PolicyCondition, ...] = Field(default_factory=tuple)
    rules: tuple[PolicyRule, ...] = Field(min_length=1)


class PolicySet(_PolicyModel):
    """A versioned collection of audit-gate policies."""

    version: int = Field(default=1, ge=1)
    policies: tuple[Policy, ...] = Field(default_factory=tuple)


class PolicyViolation(_PolicyModel):
    """Structured finding emitted by a denied policy rule."""

    policy_id: str
    target: PolicyTarget
    target_id: str
    path: str
    operator: PolicyOperator
    expected: Any = None
    actual: Any = None
    message: str


class PolicyEvaluationResult(_PolicyModel):
    """Policy decision returned by the evaluator."""

    allowed: bool
    decision: Literal["allow", "deny"]
    violations: tuple[PolicyViolation, ...] = Field(default_factory=tuple)

    @classmethod
    def from_violations(cls, violations: list[PolicyViolation]) -> Self:
        frozen = tuple(violations)
        return cls(
            allowed=not frozen,
            decision="allow" if not frozen else "deny",
            violations=frozen,
        )


__all__ = [
    "Policy",
    "PolicyCondition",
    "PolicyEffect",
    "PolicyEvaluationResult",
    "PolicyOperator",
    "PolicyRule",
    "PolicySet",
    "PolicyTarget",
    "PolicyViolation",
]
