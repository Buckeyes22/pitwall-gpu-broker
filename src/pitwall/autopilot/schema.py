"""Value objects for Pitwall's policy-railed Autopilot controller."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from pitwall.cost.simulator import WhatIfBatchProjection, WhatIfWorkload
    from pitwall.providers.drift import DriftFinding, DriftSeverity
    from pitwall.routing.prewarm import PrewarmRecommendation

_USD_QUANTUM = Decimal("0.000001")
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

type AutopilotOutcome = Literal["applied", "shadowed", "denied"]


class AutopilotMode(StrEnum):
    """Execution mode for Autopilot runs."""

    SHADOW = "shadow"
    APPLY = "apply"


class AutopilotActionKind(StrEnum):
    """Safe action categories the controller can reason about."""

    SET_WARM_CAPACITY = "set_warm_capacity"
    MARK_PROVIDER_UNHEALTHY = "mark_provider_unhealthy"
    RESERVE_CAPACITY = "reserve_capacity"
    ADJUST_PROVIDER_PRIORITY = "adjust_provider_priority"


@dataclass(frozen=True, slots=True)
class AutopilotHardLimits:
    """Run-level hard stops for autonomous actions."""

    max_actions_per_run: int = 5
    max_reserved_usd_per_action: Decimal | None = None
    max_reserved_usd_per_run: Decimal | None = None
    max_projected_spend_usd: Decimal | None = None

    def __post_init__(self) -> None:
        if self.max_actions_per_run < 0:
            raise ValueError("max_actions_per_run must be >= 0")
        object.__setattr__(
            self,
            "max_reserved_usd_per_action",
            _optional_usd(self.max_reserved_usd_per_action, "max_reserved_usd_per_action"),
        )
        object.__setattr__(
            self,
            "max_reserved_usd_per_run",
            _optional_usd(self.max_reserved_usd_per_run, "max_reserved_usd_per_run"),
        )
        object.__setattr__(
            self,
            "max_projected_spend_usd",
            _optional_usd(self.max_projected_spend_usd, "max_projected_spend_usd"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "max_actions_per_run": self.max_actions_per_run,
            "max_reserved_usd_per_action": _optional_decimal_to_str(
                self.max_reserved_usd_per_action
            ),
            "max_reserved_usd_per_run": _optional_decimal_to_str(self.max_reserved_usd_per_run),
            "max_projected_spend_usd": _optional_decimal_to_str(self.max_projected_spend_usd),
        }


@dataclass(frozen=True, slots=True)
class AutopilotSignal:
    """A recommendation, scorecard, or drift finding normalized for Autopilot."""

    signal_id: str
    source: str
    action_kind: AutopilotActionKind
    target_kind: str
    target_id: str
    reason: str
    priority: int = 100
    confidence: Decimal = Decimal("1")
    params: Mapping[str, object] = field(default_factory=dict)
    policy_provider: Mapping[str, object] | None = None
    policy_workloads: tuple[Mapping[str, object], ...] = field(default_factory=tuple)
    policy_capability: Mapping[str, object] | None = None
    simulation_workloads: tuple[WhatIfWorkload, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_empty(self.signal_id, "signal_id")
        _require_non_empty(self.source, "source")
        _require_non_empty(self.target_kind, "target_kind")
        _require_non_empty(self.target_id, "target_id")
        _require_non_empty(self.reason, "reason")
        confidence = _decimal(self.confidence, "confidence")
        if confidence < Decimal("0") or confidence > Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "params", _freeze_mapping(self.params))
        object.__setattr__(
            self,
            "policy_provider",
            None if self.policy_provider is None else _freeze_mapping(self.policy_provider),
        )
        object.__setattr__(
            self,
            "policy_workloads",
            tuple(_freeze_mapping(workload) for workload in self.policy_workloads),
        )
        object.__setattr__(
            self,
            "policy_capability",
            None if self.policy_capability is None else _freeze_mapping(self.policy_capability),
        )
        object.__setattr__(self, "simulation_workloads", tuple(self.simulation_workloads))

    @classmethod
    def from_prewarm_recommendation(
        cls,
        recommendation: PrewarmRecommendation,
        *,
        simulation_workloads: Iterable[WhatIfWorkload],
        policy_provider: Mapping[str, object] | None = None,
    ) -> AutopilotSignal:
        """Build a warm-capacity signal from a routing prewarm recommendation."""

        return cls(
            signal_id=f"prewarm-{recommendation.provider_id}-{recommendation.rank}",
            source="prewarm",
            action_kind=AutopilotActionKind.SET_WARM_CAPACITY,
            target_kind="provider",
            target_id=recommendation.provider_id,
            reason=recommendation.reason,
            priority=recommendation.rank,
            confidence=Decimal("0.90"),
            params={
                "capability_id": recommendation.capability_id,
                "provider_type": recommendation.provider_type,
                "target_kind": recommendation.target_kind.value,
                "target_count": recommendation.target_count,
                "current_warm_count": recommendation.current_warm_count,
                "delta": recommendation.delta,
                "ready_by": recommendation.ready_by,
                "expires_at": recommendation.expires_at,
                "target": dict(recommendation.target),
            },
            policy_provider=policy_provider,
            simulation_workloads=tuple(simulation_workloads),
        )

    @classmethod
    def from_drift_finding(
        cls,
        finding: DriftFinding,
        *,
        simulation_workloads: Iterable[WhatIfWorkload],
        policy_provider: Mapping[str, object] | None = None,
    ) -> AutopilotSignal:
        """Build a provider-health action signal from a drift finding."""

        return cls(
            signal_id=f"drift-{finding.provider_id}-{finding.field}",
            source="drift",
            action_kind=_action_kind_for_drift(finding),
            target_kind="provider",
            target_id=finding.provider_id,
            reason=finding.message or f"{finding.field} drift detected",
            priority=_priority_for_drift(finding.severity),
            confidence=_confidence_for_drift(finding.severity),
            params={
                "field": finding.field,
                "expected": finding.expected,
                "observed": finding.observed,
                "severity": finding.severity.value,
                "message": finding.message,
            },
            policy_provider=policy_provider,
            simulation_workloads=tuple(simulation_workloads),
        )


@dataclass(frozen=True, slots=True)
class AutopilotAction:
    """A deterministic action proposal derived from one signal."""

    action_id: str
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

    @classmethod
    def from_signal(cls, signal: AutopilotSignal) -> AutopilotAction:
        return cls(
            action_id=f"ap-{signal.signal_id}",
            signal_id=signal.signal_id,
            source=signal.source,
            action_kind=signal.action_kind,
            target_kind=signal.target_kind,
            target_id=signal.target_id,
            reason=signal.reason,
            priority=signal.priority,
            confidence=signal.confidence,
            params=signal.params,
            policy_provider=signal.policy_provider,
            policy_workloads=signal.policy_workloads,
            policy_capability=signal.policy_capability,
            simulation_workloads=signal.simulation_workloads,
        )

    def __post_init__(self) -> None:
        _require_non_empty(self.action_id, "action_id")
        _require_non_empty(self.signal_id, "signal_id")
        object.__setattr__(self, "params", _freeze_mapping(self.params))
        object.__setattr__(
            self,
            "policy_provider",
            None if self.policy_provider is None else _freeze_mapping(self.policy_provider),
        )
        object.__setattr__(
            self,
            "policy_workloads",
            tuple(_freeze_mapping(workload) for workload in self.policy_workloads),
        )
        object.__setattr__(
            self,
            "policy_capability",
            None if self.policy_capability is None else _freeze_mapping(self.policy_capability),
        )
        object.__setattr__(self, "simulation_workloads", tuple(self.simulation_workloads))

    def policy_provider_snapshot(self) -> Mapping[str, object]:
        if self.policy_provider is not None:
            return self.policy_provider
        return {
            "id": self.target_id,
            "provider_type": self.action_kind.value,
            "config": {"autopilot": dict(self.params)},
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "signal_id": self.signal_id,
            "source": self.source,
            "action_kind": self.action_kind.value,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "reason": self.reason,
            "priority": self.priority,
            "confidence": str(self.confidence),
            "params": _safe_value(self.params),
        }


@dataclass(frozen=True, slots=True)
class AutopilotGateResult:
    """Audit result for one controller gate."""

    name: str
    passed: bool
    reason: str
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.name, "name")
        _require_non_empty(self.reason, "reason")
        object.__setattr__(self, "details", _freeze_mapping(self.details))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "reason": self.reason,
            "details": _safe_value(self.details),
        }


@dataclass(frozen=True, slots=True)
class ActionApplyResult:
    """Executor result captured in the audit trail."""

    action_id: str
    applied: bool
    message: str
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.action_id, "action_id")
        _require_non_empty(self.message, "message")
        object.__setattr__(self, "details", _freeze_mapping(self.details))

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "applied": self.applied,
            "message": self.message,
            "details": _safe_value(self.details),
        }


@dataclass(frozen=True, slots=True)
class AutopilotDecision:
    """One audited action decision."""

    action: AutopilotAction
    outcome: AutopilotOutcome
    gates: tuple[AutopilotGateResult, ...]
    simulation: WhatIfBatchProjection | None = None
    apply_result: ActionApplyResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "gates", tuple(self.gates))

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "action": self.action.to_dict(),
            "outcome": self.outcome,
            "gates": [gate.to_dict() for gate in self.gates],
            "simulation": None if self.simulation is None else self.simulation.to_dict(),
            "apply_result": (None if self.apply_result is None else self.apply_result.to_dict()),
        }
        return payload


@dataclass(frozen=True, slots=True)
class AutopilotRunResult:
    """Full audit trail for one Autopilot control-loop iteration."""

    now: dt.datetime
    mode: AutopilotMode
    limits: AutopilotHardLimits
    decisions: tuple[AutopilotDecision, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "now", _normalize_utc(self.now, "now"))
        object.__setattr__(self, "decisions", tuple(self.decisions))

    @property
    def applied_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.outcome == "applied")

    def to_dict(self) -> dict[str, object]:
        return {
            "now": _isoformat_utc(self.now),
            "mode": self.mode.value,
            "limits": self.limits.to_dict(),
            "applied_count": self.applied_count,
            "decisions": [decision.to_dict() for decision in self.decisions],
        }


def _priority_for_drift(severity: DriftSeverity) -> int:
    severity_value = severity.value
    if severity_value == "critical":
        return 0
    if severity_value == "high":
        return 10
    if severity_value == "medium":
        return 50
    if severity_value == "low":
        return 80
    return 100


def _confidence_for_drift(severity: DriftSeverity) -> Decimal:
    severity_value = severity.value
    if severity_value in {"critical", "high"}:
        return Decimal("0.95")
    if severity_value == "medium":
        return Decimal("0.85")
    return Decimal("0.75")


def _action_kind_for_drift(finding: DriftFinding) -> AutopilotActionKind:
    if finding.field in {"availability", "health_status", "provider_id", "enabled"}:
        return AutopilotActionKind.MARK_PROVIDER_UNHEALTHY
    return AutopilotActionKind.ADJUST_PROVIDER_PRIORITY


def _require_non_empty(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _normalize_utc(value: dt.datetime, field_name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(dt.UTC)


def _isoformat_utc(value: dt.datetime) -> str:
    return _normalize_utc(value, "datetime").isoformat()


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(sorted(value.items())))


def _optional_usd(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    return _usd(value, field_name)


def _usd(value: object, field_name: str) -> Decimal:
    amount = _decimal(value, field_name)
    if amount < Decimal("0"):
        raise ValueError(f"{field_name} must be >= 0")
    return amount.quantize(_USD_QUANTUM)


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int | str):
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"{field_name} must be decimal-compatible") from exc
    else:
        raise ValueError(f"{field_name} must be decimal-compatible")
    if not amount.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return amount


def _optional_decimal_to_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _safe_value(value: object, *, field_name: str = "") -> object:
    if _is_sensitive_field(field_name):
        return "<redacted>"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return _isoformat_utc(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _safe_value(item, field_name=str(key))
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return [_safe_value(item, field_name=field_name) for item in value]
    if isinstance(value, str):
        return _safe_url(value)
    return value


def _safe_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    has_userinfo = parsed.username is not None or parsed.password is not None
    if not parsed.query and not parsed.fragment and not has_userinfo:
        return value
    netloc = parsed.netloc.rsplit("@", 1)[-1] if has_userinfo else parsed.netloc
    query = "<redacted>" if parsed.query or parsed.fragment else ""
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def _is_sensitive_field(field_name: str) -> bool:
    normalized = field_name.lower()
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


__all__ = [
    "ActionApplyResult",
    "AutopilotAction",
    "AutopilotActionKind",
    "AutopilotDecision",
    "AutopilotGateResult",
    "AutopilotHardLimits",
    "AutopilotMode",
    "AutopilotOutcome",
    "AutopilotRunResult",
    "AutopilotSignal",
]
