"""Pre-spend capability audit service.

The service backs ``POST /v1/admin/audit-capability/{name}`` but remains
framework-free so the REST and MCP layers can share one implementation.  It
only reads Pitwall state, runs local estimators, and invokes the hermetic
18-check audit harness; it never calls RunPod execution APIs.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from pitwall.audit._runtime_config import RuntimeAuditConfig
from pitwall.audit.sixteen_check import (
    EXPECTED_AUDIT_CHECK_COUNT,
    PreSpendDecision,
    PreSpendPayloadScanResult,
    run_all_checks,
    scan_pre_spend_payload,
)
from pitwall.audit.sixteen_check import CheckResult as SixteenCheckResult
from pitwall.core.models import Capability, Provider
from pitwall.cost.estimator import EstimatePayload, get_estimator

CHECK_CAPABILITY_EXISTS = "capability_exists"
CHECK_PROVIDER_CHAIN_NONEMPTY = "provider_chain_nonempty"
CHECK_ALL_PROVIDERS_HEALTHY = "all_providers_healthy"
CHECK_PRE_SPEND_PAYLOAD_GUARDRAIL = "pre_spend_payload_guardrail"
CHECK_COST_ESTIMATE_UNDER_CAP = "cost_estimate_under_cap"
CHECK_MONTHLY_BUDGET_HEADROOM = "monthly_budget_headroom"
CHECK_SIXTEEN_CHECK_AUDIT_PASSED = "16_check_runpod_audit_passed"
CHECK_READY_TO_INVOKE = "ready_to_invoke"

REQUIRED_CHECK_NAMES = (
    CHECK_CAPABILITY_EXISTS,
    CHECK_PROVIDER_CHAIN_NONEMPTY,
    CHECK_ALL_PROVIDERS_HEALTHY,
    CHECK_PRE_SPEND_PAYLOAD_GUARDRAIL,
    CHECK_COST_ESTIMATE_UNDER_CAP,
    CHECK_MONTHLY_BUDGET_HEADROOM,
    CHECK_SIXTEEN_CHECK_AUDIT_PASSED,
    CHECK_READY_TO_INVOKE,
)

HEALTHY_PROVIDER_STATUSES = frozenset({"healthy"})
ADMITTED_WORKLOAD_STATES = ("queued", "running", "completed")

_DEFAULT_PROVIDER_LIMIT = 1_000
_PITWALL_MONTHLY_BUDGET_USD = "PITWALL_MONTHLY_BUDGET_USD"
_PITWALL_PER_REQUEST_MAX_USD = "PITWALL_PER_REQUEST_MAX_USD"
_USD_QUANTUM = Decimal("0.000001")


class CapabilityReader(Protocol):
    async def get_by_name(self, name: str) -> Capability | None: ...


class ProviderReader(Protocol):
    async def list(
        self,
        *,
        capability_id: str | None = None,
        enabled_only: bool = False,
        provider_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Provider]: ...


SixteenCheckRunner = Callable[[Any], Sequence[SixteenCheckResult]]
AuditConfigFactory = Callable[[], Any]
NowFactory = Callable[[], dt.datetime]


@dataclass(frozen=True, slots=True)
class ProviderEstimate:
    provider_id: str
    estimate_usd: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "estimate_usd": _decimal_to_json(self.estimate_usd),
        }


@dataclass(frozen=True, slots=True)
class CapabilityAuditCheck:
    name: str
    passed: bool
    message: str = ""
    warnings: tuple[str, ...] = ()
    estimated_usd: Decimal | None = None
    remaining_usd: Decimal | None = None
    checked_at: dt.datetime | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def pass_(self) -> bool:
        """Python-safe alias for the JSON ``pass`` field."""

        return self.passed

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "pass": self.passed,
            "warnings": list(self.warnings),
        }
        if self.message:
            payload["message"] = self.message
        if self.estimated_usd is not None:
            payload["estimated_usd"] = _decimal_to_json(self.estimated_usd)
        if self.remaining_usd is not None:
            payload["remaining_usd"] = _decimal_to_json(self.remaining_usd)
        if self.checked_at is not None:
            payload["checked_at"] = self.checked_at.isoformat()
        if self.details:
            payload["details"] = _json_safe(self.details)
        return payload

    def model_dump(self, **_: Any) -> dict[str, Any]:
        """Pydantic-style convenience used by route tests and handlers."""

        return self.to_dict()


@dataclass(frozen=True, slots=True)
class CapabilityAuditResult:
    capability_name: str
    checks: tuple[CapabilityAuditCheck, ...]
    ready_to_invoke: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability_name,
            "capability_name": self.capability_name,
            "checks": [check.to_dict() for check in self.checks],
            "ready_to_invoke": self.ready_to_invoke,
        }

    def model_dump(self, **_: Any) -> dict[str, Any]:
        """Pydantic-style convenience used by route tests and handlers."""

        return self.to_dict()


@dataclass(frozen=True, slots=True)
class BudgetState:
    monthly_budget_usd: Decimal
    per_request_max_usd: Decimal
    month_to_date_spend_usd: Decimal = Decimal("0")

    @property
    def remaining_before_estimate_usd(self) -> Decimal:
        remaining = self.monthly_budget_usd - self.month_to_date_spend_usd
        return remaining if remaining > 0 else Decimal("0")


class CapabilityAuditService:
    """Run the eight required capability pre-spend checks."""

    def __init__(
        self,
        *,
        capability_repo: CapabilityReader,
        provider_repo: ProviderReader,
        pool: Any | None = None,
        budget_state: BudgetState | None = None,
        environ: Mapping[str, str] | None = None,
        sixteen_check_runner: SixteenCheckRunner = run_all_checks,
        audit_config_factory: AuditConfigFactory = RuntimeAuditConfig,
        now_factory: NowFactory | None = None,
    ) -> None:
        self._capability_repo = capability_repo
        self._provider_repo = provider_repo
        self._pool = pool
        self._budget_state = budget_state
        self._environ = environ if environ is not None else os.environ
        self._sixteen_check_runner = sixteen_check_runner
        self._audit_config_factory = audit_config_factory
        self._now_factory = now_factory or (lambda: dt.datetime.now(dt.UTC))

    async def audit(
        self,
        capability_name: str,
        *,
        payload: EstimatePayload | None = None,
    ) -> CapabilityAuditResult:
        """Return all eight checks for *capability_name*.

        The method deliberately does not short-circuit. Operators should see a
        complete failure picture even when the first check fails.
        """

        raw_payload = payload or {}
        payload_guardrail = scan_pre_spend_payload(raw_payload)
        estimate_payload = _estimate_payload_from_guardrail(payload_guardrail)
        checked_at = self._as_utc(self._now_factory())

        capability = await self._capability_repo.get_by_name(capability_name)
        providers = await self._providers_for_capability(capability)
        if payload_guardrail.blocked:
            estimates: tuple[ProviderEstimate, ...] = ()
            estimate_warnings: tuple[str, ...] = (
                "pre-spend payload guardrail blocked the request before cost estimation",
            )
        else:
            estimates, estimate_warnings = self._estimate_providers(
                capability,
                providers,
                estimate_payload,
            )

        checks = [
            self._check_capability_exists(capability_name, capability),
            self._check_provider_chain_nonempty(providers),
            self._check_all_providers_healthy(providers),
            self._check_pre_spend_payload_guardrail(payload_guardrail),
            self._check_cost_estimate_under_cap(estimates, estimate_warnings),
            await self._check_monthly_budget_headroom(estimates, estimate_warnings),
            self._check_sixteen_check_runpod_audit_passed(checked_at),
        ]
        ready_check = self._check_ready_to_invoke(capability, checks)
        checks.append(ready_check)

        return CapabilityAuditResult(
            capability_name=capability_name,
            checks=tuple(checks),
            ready_to_invoke=ready_check.passed,
        )

    async def _providers_for_capability(
        self,
        capability: Capability | None,
    ) -> tuple[Provider, ...]:
        if capability is None:
            return ()
        providers = await self._provider_repo.list(
            capability_id=capability.id,
            enabled_only=True,
            provider_type=None,
            limit=_DEFAULT_PROVIDER_LIMIT,
            offset=0,
        )
        return tuple(providers)

    def _estimate_providers(
        self,
        capability: Capability | None,
        providers: Sequence[Provider],
        payload: EstimatePayload,
    ) -> tuple[tuple[ProviderEstimate, ...], tuple[str, ...]]:
        if capability is None:
            return (), ("capability is missing",)
        if not providers:
            return (), ("provider chain is empty",)

        estimator = get_estimator(capability.cost_mode)
        estimates: list[ProviderEstimate] = []
        warnings: list[str] = []
        for provider in providers:
            try:
                estimate = estimator.estimate(capability, provider.config, payload)
            except ValueError as exc:
                warnings.append(f"{provider.id}: {exc}")
                continue
            estimates.append(
                ProviderEstimate(
                    provider_id=provider.id,
                    estimate_usd=estimate.quantize(_USD_QUANTUM),
                )
            )
        return tuple(estimates), tuple(warnings)

    def _check_capability_exists(
        self,
        capability_name: str,
        capability: Capability | None,
    ) -> CapabilityAuditCheck:
        if capability is None:
            return CapabilityAuditCheck(
                name=CHECK_CAPABILITY_EXISTS,
                passed=False,
                message=f"capability {capability_name!r} was not found",
            )

        warnings: tuple[str, ...] = ()
        if not capability.enabled:
            warnings = ("capability is disabled",)
        return CapabilityAuditCheck(
            name=CHECK_CAPABILITY_EXISTS,
            passed=True,
            message=f"capability {capability.name!r} exists",
            warnings=warnings,
            details={"capability_id": capability.id, "enabled": capability.enabled},
        )

    def _check_provider_chain_nonempty(
        self,
        providers: Sequence[Provider],
    ) -> CapabilityAuditCheck:
        return CapabilityAuditCheck(
            name=CHECK_PROVIDER_CHAIN_NONEMPTY,
            passed=bool(providers),
            message=(
                f"{len(providers)} enabled provider(s) found"
                if providers
                else "no enabled providers found for capability"
            ),
            details={"provider_ids": [provider.id for provider in providers]},
        )

    def _check_all_providers_healthy(
        self,
        providers: Sequence[Provider],
    ) -> CapabilityAuditCheck:
        unhealthy = [
            provider
            for provider in providers
            if provider.health_status.lower() not in HEALTHY_PROVIDER_STATUSES
        ]
        warnings = tuple(
            f"{provider.id}: health_status={provider.health_status!r}" for provider in unhealthy
        )
        return CapabilityAuditCheck(
            name=CHECK_ALL_PROVIDERS_HEALTHY,
            passed=bool(providers) and not unhealthy,
            message=(
                "all enabled providers are healthy"
                if providers and not unhealthy
                else "one or more enabled providers are not healthy"
            ),
            warnings=warnings,
            details={
                "provider_health": {provider.id: provider.health_status for provider in providers},
            },
        )

    def _check_pre_spend_payload_guardrail(
        self,
        result: PreSpendPayloadScanResult,
    ) -> CapabilityAuditCheck:
        if result.decision == PreSpendDecision.ALLOW:
            message = "pre-spend payload contains no configured PII or secret findings"
        elif result.decision == PreSpendDecision.REDACT:
            message = "pre-spend payload was redacted before cost estimation"
        else:
            message = "pre-spend payload was blocked before cost estimation"
        return CapabilityAuditCheck(
            name=CHECK_PRE_SPEND_PAYLOAD_GUARDRAIL,
            passed=not result.blocked,
            message=message,
            warnings=(
                ("payload redacted before downstream processing",)
                if result.decision == PreSpendDecision.REDACT
                else ()
            ),
            details=result.to_dict(),
        )

    def _check_cost_estimate_under_cap(
        self,
        estimates: Sequence[ProviderEstimate],
        estimate_warnings: Sequence[str],
    ) -> CapabilityAuditCheck:
        cap = self._per_request_cap()
        if cap is None:
            return CapabilityAuditCheck(
                name=CHECK_COST_ESTIMATE_UNDER_CAP,
                passed=False,
                message=f"{_PITWALL_PER_REQUEST_MAX_USD} is not configured",
                warnings=tuple(estimate_warnings),
            )
        if not estimates:
            if "pre-spend payload guardrail blocked" in " ".join(estimate_warnings):
                return CapabilityAuditCheck(
                    name=CHECK_COST_ESTIMATE_UNDER_CAP,
                    passed=False,
                    message=(
                        "cost estimate skipped because pre-spend payload guardrail "
                        "blocked the request"
                    ),
                    warnings=tuple(estimate_warnings),
                )
            return CapabilityAuditCheck(
                name=CHECK_COST_ESTIMATE_UNDER_CAP,
                passed=False,
                message="no provider cost estimate is available",
                warnings=tuple(estimate_warnings),
            )

        over_cap = [estimate for estimate in estimates if estimate.estimate_usd > cap]
        selected_estimate = estimates[0].estimate_usd
        warnings = [*estimate_warnings]
        warnings.extend(
            f"{estimate.provider_id}: estimated_usd={estimate.estimate_usd} > cap={cap}"
            for estimate in over_cap
        )
        return CapabilityAuditCheck(
            name=CHECK_COST_ESTIMATE_UNDER_CAP,
            passed=not over_cap,
            message=(
                f"all provider estimates are <= per-request cap {cap}"
                if not over_cap
                else "one or more provider estimates exceed the per-request cap"
            ),
            warnings=tuple(warnings),
            estimated_usd=selected_estimate,
            details={
                "per_request_max_usd": _decimal_to_json(cap),
                "provider_estimates": [estimate.to_dict() for estimate in estimates],
            },
        )

    async def _check_monthly_budget_headroom(
        self,
        estimates: Sequence[ProviderEstimate],
        estimate_warnings: Sequence[str],
    ) -> CapabilityAuditCheck:
        if not estimates:
            return CapabilityAuditCheck(
                name=CHECK_MONTHLY_BUDGET_HEADROOM,
                passed=False,
                message="monthly headroom cannot be evaluated without a cost estimate",
                warnings=tuple(estimate_warnings),
            )

        budget_state = await self._resolve_budget_state()
        if budget_state is None:
            return CapabilityAuditCheck(
                name=CHECK_MONTHLY_BUDGET_HEADROOM,
                passed=False,
                message=f"{_PITWALL_MONTHLY_BUDGET_USD} is not configured",
            )

        required = max(estimate.estimate_usd for estimate in estimates)
        remaining_before = budget_state.remaining_before_estimate_usd
        remaining_after = remaining_before - required
        passed = remaining_after >= 0
        return CapabilityAuditCheck(
            name=CHECK_MONTHLY_BUDGET_HEADROOM,
            passed=passed,
            message=(
                "monthly budget has enough headroom"
                if passed
                else "monthly budget does not have enough headroom"
            ),
            remaining_usd=remaining_after if remaining_after > 0 else Decimal("0"),
            details={
                "monthly_budget_usd": _decimal_to_json(budget_state.monthly_budget_usd),
                "month_to_date_spend_usd": _decimal_to_json(budget_state.month_to_date_spend_usd),
                "remaining_before_estimate_usd": _decimal_to_json(remaining_before),
                "required_estimate_usd": _decimal_to_json(required),
            },
        )

    def _check_sixteen_check_runpod_audit_passed(
        self,
        checked_at: dt.datetime,
    ) -> CapabilityAuditCheck:
        results = tuple(self._sixteen_check_runner(self._audit_config_factory()))
        failed = [result for result in results if not result.passed]
        warnings = [f"#{result.check_id} {result.name}: {result.message}" for result in failed]
        if len(results) != EXPECTED_AUDIT_CHECK_COUNT:
            warnings.append(
                f"expected {EXPECTED_AUDIT_CHECK_COUNT} RunPod audit checks, got {len(results)}"
            )
        passed = len(results) == EXPECTED_AUDIT_CHECK_COUNT and not failed
        passed_count = len(results) - len(failed)
        return CapabilityAuditCheck(
            name=CHECK_SIXTEEN_CHECK_AUDIT_PASSED,
            passed=passed,
            message=f"{passed_count}/{len(results)} RunPod audit checks passed",
            warnings=tuple(warnings),
            checked_at=checked_at,
            details={
                "passed": passed_count,
                "total": len(results),
                "checks": [_sixteen_check_result_to_dict(result) for result in results],
            },
        )

    def _check_ready_to_invoke(
        self,
        capability: Capability | None,
        checks: Sequence[CapabilityAuditCheck],
    ) -> CapabilityAuditCheck:
        warnings: list[str] = []
        if capability is not None and not capability.enabled:
            warnings.append("capability is disabled")
        failing = [check.name for check in checks if not check.passed]
        warnings.extend(f"{name} failed" for name in failing)
        passed = capability is not None and capability.enabled and not failing
        return CapabilityAuditCheck(
            name=CHECK_READY_TO_INVOKE,
            passed=passed,
            message=(
                "capability is ready to invoke" if passed else "capability is not ready to invoke"
            ),
            warnings=tuple(warnings),
        )

    async def _resolve_budget_state(self) -> BudgetState | None:
        if self._budget_state is not None:
            return self._budget_state

        monthly_budget = _optional_positive_decimal_env(
            self._environ,
            _PITWALL_MONTHLY_BUDGET_USD,
        )
        per_request_cap = _optional_positive_decimal_env(
            self._environ,
            _PITWALL_PER_REQUEST_MAX_USD,
        )
        if monthly_budget is None or per_request_cap is None:
            return None

        month_to_date = Decimal("0")
        if self._pool is not None:
            month_to_date = await read_month_to_date_spend_usd(self._pool)

        return BudgetState(
            monthly_budget_usd=monthly_budget,
            per_request_max_usd=per_request_cap,
            month_to_date_spend_usd=month_to_date,
        )

    def _per_request_cap(self) -> Decimal | None:
        if self._budget_state is not None:
            return self._budget_state.per_request_max_usd
        return _optional_positive_decimal_env(
            self._environ,
            _PITWALL_PER_REQUEST_MAX_USD,
        )

    @staticmethod
    def _as_utc(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)


async def audit_capability(
    capability_name: str,
    *,
    capability_repo: CapabilityReader,
    provider_repo: ProviderReader,
    pool: Any | None = None,
    payload: EstimatePayload | None = None,
    budget_state: BudgetState | None = None,
    environ: Mapping[str, str] | None = None,
    sixteen_check_runner: SixteenCheckRunner = run_all_checks,
    audit_config_factory: AuditConfigFactory = RuntimeAuditConfig,
    now_factory: NowFactory | None = None,
) -> CapabilityAuditResult:
    """Convenience wrapper around :class:`CapabilityAuditService`."""

    service = CapabilityAuditService(
        capability_repo=capability_repo,
        provider_repo=provider_repo,
        pool=pool,
        budget_state=budget_state,
        environ=environ,
        sixteen_check_runner=sixteen_check_runner,
        audit_config_factory=audit_config_factory,
        now_factory=now_factory,
    )
    return await service.audit(capability_name, payload=payload)


async def read_month_to_date_spend_usd(pool: Any) -> Decimal:
    """Read the same month-to-date spend window used by ``BudgetGate``.

    The query is read-only and mirrors the admission states used by
    :class:`pitwall.cost.budget_gate.BudgetGate`.
    """

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT COALESCE(SUM(cost_estimate_usd), 0) AS s
               FROM pitwall.workloads
               WHERE submitted_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
                 AND state IN ('queued','running','completed')"""
        )
    return _required_decimal(_row_get(row, "s", Decimal("0")), "month_to_date_spend_usd")


def _optional_positive_decimal_env(
    environ: Mapping[str, str],
    name: str,
) -> Decimal | None:
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    value = _required_decimal(raw, name)
    if value <= 0:
        return None
    return value.quantize(_USD_QUANTUM)


def _required_decimal(raw_value: object, name: str) -> Decimal:
    if isinstance(raw_value, bool):
        raise ValueError(f"{name} must be a decimal value")
    try:
        value = Decimal(str(raw_value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal value") from exc
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _estimate_payload_from_guardrail(result: PreSpendPayloadScanResult) -> EstimatePayload:
    payload = result.redacted_payload
    if not isinstance(payload, Mapping):
        return {}
    return {str(key): value for key, value in payload.items()}


def _decimal_to_json(value: Decimal) -> str:
    return format(value.quantize(_USD_QUANTUM), "f")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_to_json(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


def _sixteen_check_result_to_dict(result: SixteenCheckResult) -> dict[str, Any]:
    severity = getattr(result.severity, "value", result.severity)
    return {
        "check_id": result.check_id,
        "name": result.name,
        "passed": result.passed,
        "severity": str(severity),
        "evidence": result.evidence,
        "remediation": result.remediation,
        "message": result.message,
    }


__all__ = [
    "ADMITTED_WORKLOAD_STATES",
    "CHECK_ALL_PROVIDERS_HEALTHY",
    "CHECK_CAPABILITY_EXISTS",
    "CHECK_COST_ESTIMATE_UNDER_CAP",
    "CHECK_MONTHLY_BUDGET_HEADROOM",
    "CHECK_PRE_SPEND_PAYLOAD_GUARDRAIL",
    "CHECK_PROVIDER_CHAIN_NONEMPTY",
    "CHECK_READY_TO_INVOKE",
    "CHECK_SIXTEEN_CHECK_AUDIT_PASSED",
    "HEALTHY_PROVIDER_STATUSES",
    "REQUIRED_CHECK_NAMES",
    "BudgetState",
    "CapabilityAuditCheck",
    "CapabilityAuditResult",
    "CapabilityAuditService",
    "ProviderEstimate",
    "audit_capability",
    "read_month_to_date_spend_usd",
]
