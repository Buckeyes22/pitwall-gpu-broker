"""Deterministic Policy-as-Code evaluator for Pitwall audit inputs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from pydantic import BaseModel

from pitwall.policy.schema import (
    Policy,
    PolicyCondition,
    PolicyEvaluationResult,
    PolicyOperator,
    PolicyRule,
    PolicySet,
    PolicyTarget,
    PolicyViolation,
)


@dataclass(frozen=True, slots=True)
class _MissingValue:
    pass


@dataclass(frozen=True, slots=True)
class _TargetRecord:
    target: PolicyTarget
    target_id: str
    data: object


_MISSING = _MissingValue()
_SENSITIVE_KEY_FRAGMENTS = (
    "access_key",
    "api_key",
    "authorization",
    "bearer",
    "credential",
    "password",
    "secret",
    "session_token",
    "token",
)


def evaluate_policies(policy_set: PolicySet, config: object) -> PolicyEvaluationResult:
    """Evaluate *policy_set* against *config*, returning an allow/deny decision."""

    violations: list[PolicyViolation] = []
    for policy in policy_set.policies:
        for target in _targets_for_policy(config, policy):
            if not _conditions_match(policy.when, target.data):
                continue
            for rule in policy.rules:
                if _condition_passes(rule, target.data):
                    continue
                actual = _read_path(target.data, rule.path)
                violations.append(
                    PolicyViolation(
                        policy_id=policy.id,
                        target=target.target,
                        target_id=target.target_id,
                        path=rule.path,
                        operator=rule.operator,
                        expected=_safe_value(rule.value),
                        actual=_safe_value(actual, field_name=rule.path.rsplit(".", 1)[-1]),
                        message=rule.message
                        or _default_violation_message(policy, rule, target.target_id),
                    )
                )
    return PolicyEvaluationResult.from_violations(violations)


def _targets_for_policy(config: object, policy: Policy) -> tuple[_TargetRecord, ...]:
    if policy.target == PolicyTarget.CAPABILITY:
        capability = _value_for_key(config, "capability")
        if capability is _MISSING or capability is None:
            return ()
        return (
            _TargetRecord(
                target=PolicyTarget.CAPABILITY,
                target_id=_target_id(
                    capability,
                    fallback="capability",
                    field_names=("id", "name", "capability_id"),
                ),
                data=capability,
            ),
        )

    if policy.target == PolicyTarget.PROVIDER:
        providers = _sequence_from_config(config, "provider_fixtures", "providers")
        return tuple(
            _TargetRecord(
                target=PolicyTarget.PROVIDER,
                target_id=_target_id(
                    provider,
                    fallback=f"provider[{index}]",
                    field_names=("id", "provider_id", "name", "capability_id"),
                ),
                data=provider,
            )
            for index, provider in enumerate(providers)
        )

    workloads = _sequence_from_config(config, "workloads", "workload_fixtures")
    return tuple(
        _TargetRecord(
            target=PolicyTarget.WORKLOAD,
            target_id=_target_id(
                workload,
                fallback=f"workload[{index}]",
                field_names=("id",),
            ),
            data=workload,
        )
        for index, workload in enumerate(workloads)
    )


def _sequence_from_config(config: object, primary: str, secondary: str) -> tuple[object, ...]:
    value = _value_for_key(config, primary)
    if value is _MISSING:
        value = _value_for_key(config, secondary)
    if value is _MISSING or value is None:
        return ()
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return tuple(value)
    return (value,)


def _conditions_match(conditions: Iterable[PolicyCondition], data: object) -> bool:
    return all(_condition_passes(condition, data) for condition in conditions)


def _condition_passes(condition: PolicyCondition, data: object) -> bool:
    actual = _read_path(data, condition.path)
    expected = condition.value
    operator = condition.operator

    if operator == PolicyOperator.EXISTS:
        return actual is not _MISSING and actual is not None
    if operator == PolicyOperator.NOT_EXISTS:
        return actual is _MISSING or actual is None
    if actual is _MISSING:
        return operator == PolicyOperator.CONTAINS_NONE
    if operator == PolicyOperator.EQUALS:
        return _normalize_for_compare(actual) == _normalize_for_compare(expected)
    if operator == PolicyOperator.NOT_EQUALS:
        return _normalize_for_compare(actual) != _normalize_for_compare(expected)
    if operator == PolicyOperator.IN:
        return _normalize_scalar(actual) in _expected_members(expected)
    if operator == PolicyOperator.NOT_IN:
        return _normalize_scalar(actual) not in _expected_members(expected)
    if operator == PolicyOperator.CONTAINS:
        return _normalize_scalar(expected) in _collection_members(actual)
    if operator == PolicyOperator.NOT_CONTAINS:
        return _normalize_scalar(expected) not in _collection_members(actual)
    if operator == PolicyOperator.CONTAINS_ALL:
        members = _collection_members(actual)
        return all(member in members for member in _expected_members(expected))
    if operator == PolicyOperator.CONTAINS_NONE:
        members = _collection_members(actual)
        return not any(member in members for member in _expected_members(expected))
    if operator == PolicyOperator.GTE:
        return _decimal_compare(actual, expected, greater_or_equal=True)
    if operator == PolicyOperator.LTE:
        return _decimal_compare(actual, expected, greater_or_equal=False)
    if operator == PolicyOperator.MATCHES:
        return re.search(str(expected), str(_normalize_scalar(actual))) is not None
    return False


def _read_path(source: object, path: str) -> object:
    current: object = source
    for segment in path.split("."):
        current = _python_value(current)
        if isinstance(current, Mapping):
            if segment not in current:
                return _MISSING
            current = current[segment]
            continue
        if hasattr(current, segment):
            current = getattr(current, segment)
            continue
        return _MISSING
    return current


def _value_for_key(source: object, key: str) -> object:
    value = _read_path(source, key)
    if callable(value):
        return value()
    return value


def _target_id(target: object, *, fallback: str, field_names: tuple[str, ...]) -> str:
    for field_name in field_names:
        value = _read_path(target, field_name)
        if value is not _MISSING and value is not None and str(value):
            return str(_normalize_scalar(value))
    return fallback


def _python_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    return value


def _normalize_for_compare(value: object) -> object:
    value = _python_value(value)
    if isinstance(value, Mapping):
        return {str(key): _normalize_for_compare(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_normalize_for_compare(item) for item in value]
    return _normalize_scalar(value)


def _normalize_scalar(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    return value


def _expected_members(value: object) -> tuple[object, ...]:
    if isinstance(value, Mapping):
        return tuple(_normalize_scalar(key) for key in value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return tuple(_normalize_scalar(item) for item in value)
    if value is None:
        return ()
    return (_normalize_scalar(value),)


def _collection_members(value: object) -> tuple[object, ...]:
    value = _python_value(value)
    if value is _MISSING or value is None:
        return ()
    if isinstance(value, Mapping):
        return tuple(_normalize_scalar(key) for key in value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return tuple(_normalize_scalar(item) for item in value)
    return (_normalize_scalar(value),)


def _decimal_compare(actual: object, expected: object, *, greater_or_equal: bool) -> bool:
    try:
        actual_decimal = _to_decimal(actual)
        expected_decimal = _to_decimal(expected)
    except ValueError:
        return False
    if greater_or_equal:
        return actual_decimal >= expected_decimal
    return actual_decimal <= expected_decimal


def _to_decimal(value: object) -> Decimal:
    value = _normalize_scalar(value)
    if isinstance(value, bool) or value is None:
        raise ValueError("not decimal-compatible")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("not decimal-compatible") from exc
    if not parsed.is_finite():
        raise ValueError("not finite")
    return parsed


def _safe_value(value: object, *, field_name: str | None = None) -> object:
    if value is _MISSING:
        return "<missing>"
    value = _python_value(value)
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]"
            if _is_sensitive_key(str(key))
            else _safe_value(item, field_name=str(key))
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_safe_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        if field_name is not None and _is_sensitive_key(field_name):
            return "[REDACTED]"
        return _preview(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _preview(value: str) -> str:
    max_chars = 128
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3] + "..."


def _default_violation_message(policy: Policy, rule: PolicyRule, target_id: str) -> str:
    return (
        f"{policy.id} failed for {policy.target.value} {target_id}: "
        f"{rule.path} must satisfy {rule.operator.value}"
    )


__all__ = ["evaluate_policies"]
