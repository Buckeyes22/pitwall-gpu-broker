"""Hermetic tests for the capability pre-spend audit service."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from pitwall.audit.capability import (
    CHECK_ALL_PROVIDERS_HEALTHY,
    CHECK_CAPABILITY_EXISTS,
    CHECK_COST_ESTIMATE_UNDER_CAP,
    CHECK_MONTHLY_BUDGET_HEADROOM,
    CHECK_PRE_SPEND_PAYLOAD_GUARDRAIL,
    CHECK_READY_TO_INVOKE,
    CHECK_SIXTEEN_CHECK_AUDIT_PASSED,
    REQUIRED_CHECK_NAMES,
    BudgetState,
    CapabilityAuditService,
)
from pitwall.audit.sixteen_check import EXPECTED_AUDIT_CHECK_COUNT, AuditSeverity, CheckResult
from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability, Provider

NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)


def _capability(*, enabled: bool = True) -> Capability:
    return Capability(
        id="cap_bge",
        name="embedding.bge-m3",
        version="1.0.0",
        class_=CapabilityClass.EMBEDDING,
        cost_mode=CostMode.PER_REQUEST,
        enabled=enabled,
        created_at=NOW,
        updated_at=NOW,
    )


def _provider(
    *,
    id: str = "prov_bge",
    health_status: str = "healthy",
    cost_usd: str = "0.010000",
) -> Provider:
    return Provider(
        id=id,
        capability_id="cap_bge",
        name=id,
        provider_type=ProviderType.SERVERLESS_LB,
        config={"cost": {"per_request": cost_usd}},
        priority=0,
        enabled=True,
        health_status=health_status,
        source=CapabilitySource.API,
        updated_at=NOW,
    )


class FakeCapabilityRepo:
    def __init__(self, capability: Capability | None) -> None:
        self.capability = capability

    async def get_by_name(self, name: str) -> Capability | None:
        if self.capability is not None and self.capability.name == name:
            return self.capability
        return None


class FakeProviderRepo:
    def __init__(self, providers: list[Provider]) -> None:
        self.providers = providers
        self.calls: list[dict[str, Any]] = []

    async def list(
        self,
        *,
        capability_id: str | None = None,
        enabled_only: bool = False,
        provider_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Provider]:
        self.calls.append(
            {
                "capability_id": capability_id,
                "enabled_only": enabled_only,
                "provider_type": provider_type,
                "limit": limit,
                "offset": offset,
            }
        )
        providers = [
            provider
            for provider in self.providers
            if capability_id is None or provider.capability_id == capability_id
        ]
        if enabled_only:
            providers = [provider for provider in providers if provider.enabled]
        return providers[offset : offset + limit]


def _sixteen_results(*, passed: bool = True) -> list[CheckResult]:
    return [
        CheckResult(
            check_id=i,
            name=f"check {i}",
            passed=passed,
            severity=AuditSeverity.LOW,
            evidence="ok" if passed else "failed",
            remediation="",
            message="ok" if passed else "failed",
        )
        for i in range(1, EXPECTED_AUDIT_CHECK_COUNT + 1)
    ]


def _service(
    *,
    capability: Capability | None = None,
    providers: list[Provider] | None = None,
    budget_state: BudgetState | None = None,
    sixteen_passed: bool = True,
) -> CapabilityAuditService:
    return CapabilityAuditService(
        capability_repo=FakeCapabilityRepo(capability if capability is not None else _capability()),
        provider_repo=FakeProviderRepo(providers if providers is not None else [_provider()]),
        budget_state=budget_state
        or BudgetState(
            monthly_budget_usd=Decimal("10.000000"),
            per_request_max_usd=Decimal("0.050000"),
            month_to_date_spend_usd=Decimal("1.000000"),
        ),
        sixteen_check_runner=lambda _cfg: _sixteen_results(passed=sixteen_passed),
        audit_config_factory=lambda: object(),
        now_factory=lambda: NOW,
    )


def _checks_by_name(result: Any) -> dict[str, Any]:
    return {check.name: check for check in result.checks}


@pytest.mark.anyio
async def test_audit_returns_required_checks_and_ready_true() -> None:
    result = await _service().audit("embedding.bge-m3")

    assert tuple(check.name for check in result.checks) == REQUIRED_CHECK_NAMES
    assert len(result.checks) == 8
    assert result.ready_to_invoke is True
    assert _checks_by_name(result)[CHECK_READY_TO_INVOKE].passed is True

    payload = result.to_dict()
    assert payload["ready_to_invoke"] is True
    assert payload["checks"][-1]["name"] == CHECK_READY_TO_INVOKE
    assert payload["checks"][-1]["pass"] is True


@pytest.mark.anyio
async def test_missing_capability_still_returns_all_checks_and_skips_provider_lookup() -> None:
    provider_repo = FakeProviderRepo([_provider()])
    service = CapabilityAuditService(
        capability_repo=FakeCapabilityRepo(None),
        provider_repo=provider_repo,
        budget_state=BudgetState(
            monthly_budget_usd=Decimal("10"),
            per_request_max_usd=Decimal("1"),
        ),
        sixteen_check_runner=lambda _cfg: _sixteen_results(),
        audit_config_factory=lambda: object(),
        now_factory=lambda: NOW,
    )

    result = await service.audit("embedding.bge-m3")
    checks = _checks_by_name(result)

    assert tuple(check.name for check in result.checks) == REQUIRED_CHECK_NAMES
    assert checks[CHECK_CAPABILITY_EXISTS].passed is False
    assert checks[CHECK_READY_TO_INVOKE].passed is False
    assert result.ready_to_invoke is False
    assert provider_repo.calls == []


@pytest.mark.anyio
async def test_unhealthy_provider_blocks_ready_to_invoke() -> None:
    result = await _service(providers=[_provider(health_status="unhealthy")]).audit(
        "embedding.bge-m3"
    )
    checks = _checks_by_name(result)

    assert checks[CHECK_ALL_PROVIDERS_HEALTHY].passed is False
    assert checks[CHECK_READY_TO_INVOKE].passed is False
    assert result.ready_to_invoke is False


@pytest.mark.anyio
async def test_cost_estimate_and_budget_headroom_are_read_only_checks() -> None:
    result = await _service(
        providers=[_provider(cost_usd="0.100000")],
        budget_state=BudgetState(
            monthly_budget_usd=Decimal("1.000000"),
            per_request_max_usd=Decimal("0.050000"),
            month_to_date_spend_usd=Decimal("0.990000"),
        ),
    ).audit("embedding.bge-m3")
    checks = _checks_by_name(result)

    assert checks[CHECK_COST_ESTIMATE_UNDER_CAP].passed is False
    assert checks[CHECK_COST_ESTIMATE_UNDER_CAP].estimated_usd == Decimal("0.100000")
    assert checks[CHECK_MONTHLY_BUDGET_HEADROOM].passed is False
    assert checks[CHECK_MONTHLY_BUDGET_HEADROOM].remaining_usd == Decimal("0")
    assert result.ready_to_invoke is False


@pytest.mark.anyio
async def test_secret_payload_blocks_before_cost_estimate_and_redacts_findings() -> None:
    result = await _service(
        providers=[_provider(cost_usd="0.010000")],
        budget_state=BudgetState(
            monthly_budget_usd=Decimal("10.000000"),
            per_request_max_usd=Decimal("0.050000"),
            month_to_date_spend_usd=Decimal("1.000000"),
        ),
    ).audit(
        "embedding.bge-m3",
        payload={"texts": ["use sk-test_1234567890abcdef1234567890abcdef"]},
    )
    checks = _checks_by_name(result)

    guard = checks[CHECK_PRE_SPEND_PAYLOAD_GUARDRAIL]
    assert guard.passed is False
    assert guard.details["decision"] == "block"
    assert guard.details["blocked"] is True
    assert guard.details["findings"][0]["path"] == "$.texts[0]"
    assert "sk-test" not in str(guard.to_dict())
    assert checks[CHECK_COST_ESTIMATE_UNDER_CAP].passed is False
    assert checks[CHECK_COST_ESTIMATE_UNDER_CAP].message == (
        "cost estimate skipped because pre-spend payload guardrail blocked the request"
    )
    assert result.ready_to_invoke is False


@pytest.mark.anyio
async def test_email_payload_is_redacted_before_cost_estimate() -> None:
    result = await _service(
        providers=[_provider(cost_usd="0.010000")],
    ).audit(
        "embedding.bge-m3",
        payload={"texts": ["contact ada.lovelace@example.com"]},
    )
    checks = _checks_by_name(result)

    guard = checks[CHECK_PRE_SPEND_PAYLOAD_GUARDRAIL]
    assert guard.passed is True
    assert guard.details["decision"] == "redact"
    assert guard.details["redacted_payload"] == {"texts": ["contact [REDACTED:email]"]}
    assert "ada.lovelace@example.com" not in str(result.to_dict())
    assert checks[CHECK_COST_ESTIMATE_UNDER_CAP].passed is True


@pytest.mark.anyio
async def test_failed_sixteen_check_blocks_ready_to_invoke() -> None:
    result = await _service(sixteen_passed=False).audit("embedding.bge-m3")
    checks = _checks_by_name(result)

    assert checks[CHECK_SIXTEEN_CHECK_AUDIT_PASSED].passed is False
    assert checks[CHECK_READY_TO_INVOKE].passed is False
    assert result.ready_to_invoke is False


@pytest.mark.anyio
async def test_sixteen_check_results_are_exposed_in_named_status() -> None:
    result = await _service(sixteen_passed=False).audit("embedding.bge-m3")
    check = _checks_by_name(result)[CHECK_SIXTEEN_CHECK_AUDIT_PASSED]

    assert check.name == "16_check_runpod_audit_passed"
    assert check.details["passed"] == 0
    assert check.details["total"] == EXPECTED_AUDIT_CHECK_COUNT
    assert check.details["checks"][0] == {
        "check_id": 1,
        "name": "check 1",
        "passed": False,
        "severity": "low",
        "evidence": "failed",
        "remediation": "",
        "message": "failed",
    }

    payload = result.to_dict()
    status_payload = {item["name"]: item for item in payload["checks"]}[
        CHECK_SIXTEEN_CHECK_AUDIT_PASSED
    ]
    assert status_payload["details"]["checks"][-1]["check_id"] == EXPECTED_AUDIT_CHECK_COUNT
    assert status_payload["details"]["checks"][-1]["passed"] is False
