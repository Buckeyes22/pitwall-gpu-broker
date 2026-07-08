"""Hermetic async tests for complexity-based cascade routing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from pitwall.routing.cascade import (
    CascadeGateDecision,
    CascadeProviderRequest,
    CascadeRoutingError,
    CascadeTier,
    route_with_cascade,
)


@dataclass(frozen=True, slots=True)
class FakeProvider:
    id: str
    model: str


@dataclass(slots=True)
class AttemptLog:
    called: list[str] = field(default_factory=list)
    gated: list[str] = field(default_factory=list)


ProviderCallable = Callable[[FakeProvider], Awaitable[str]]


def _tier(provider_id: str, cost_usd: str) -> CascadeTier[FakeProvider]:
    return CascadeTier(
        provider=FakeProvider(id=provider_id, model=f"model:{provider_id}"),
        estimated_cost_usd=Decimal(cost_usd),
    )


def _caller(log: AttemptLog) -> ProviderCallable:
    async def call(provider: FakeProvider) -> str:
        log.called.append(provider.id)
        return f"answer:{provider.id}"

    return call


def _gate_from_outcomes(
    outcomes: dict[str, bool],
    *,
    log: AttemptLog,
) -> Callable[[FakeProvider, str], Awaitable[CascadeGateDecision]]:
    async def gate(provider: FakeProvider, value: str) -> CascadeGateDecision:
        log.gated.append(provider.id)
        passed = outcomes[provider.id]
        return CascadeGateDecision(
            passed=passed,
            confidence=0.9 if passed else 0.4,
            reason=None if passed else f"{value} below confidence floor",
        )

    return gate


@pytest.mark.parametrize("passed", ["false", 1, None])
def test_gate_decision_rejects_non_bool_passed_values(passed: object) -> None:
    with pytest.raises(ValueError, match="passed must be a bool"):
        CascadeGateDecision(passed=passed)


@pytest.mark.parametrize("value", [0.1, 1.0])
def test_cascade_tier_rejects_float_estimated_cost_usd(value: float) -> None:
    with pytest.raises(ValueError, match="estimated_cost_usd must be a Decimal"):
        CascadeTier(
            provider=FakeProvider(id="cheap", model="model:cheap"),
            estimated_cost_usd=value,
        )


@pytest.mark.anyio
async def test_first_tier_pass_stops_without_escalation() -> None:
    log = AttemptLog()

    result = await route_with_cascade(
        CascadeProviderRequest(
            tiers=[_tier("cheap", "0.010"), _tier("strong", "0.050")],
            call_provider=_caller(log),
            quality_gate=_gate_from_outcomes({"cheap": True, "strong": True}, log=log),
        )
    )

    assert result.value == "answer:cheap"
    assert result.provider_id == "cheap"
    assert result.attempted_provider_ids == ("cheap",)
    assert result.total_cost_usd == Decimal("0.010")
    assert result.escalated is False
    assert log.called == ["cheap"]
    assert log.gated == ["cheap"]


@pytest.mark.anyio
async def test_gate_failure_escalates_to_next_tier_and_accumulates_cost() -> None:
    log = AttemptLog()

    result = await route_with_cascade(
        CascadeProviderRequest(
            tiers=[_tier("cheap", "0.010"), _tier("strong", "0.050")],
            call_provider=_caller(log),
            quality_gate=_gate_from_outcomes({"cheap": False, "strong": True}, log=log),
        )
    )

    assert result.value == "answer:strong"
    assert result.provider_id == "strong"
    assert result.attempted_provider_ids == ("cheap", "strong")
    assert result.total_cost_usd == Decimal("0.060")
    assert result.escalated is True
    assert [attempt.gate.passed for attempt in result.attempts] == [False, True]
    assert log.called == ["cheap", "strong"]


@pytest.mark.anyio
async def test_all_gate_failures_raise_with_attempt_and_cost_metadata() -> None:
    log = AttemptLog()

    with pytest.raises(CascadeRoutingError) as exc_info:
        await route_with_cascade(
            CascadeProviderRequest(
                tiers=[_tier("cheap", "0.010"), _tier("strong", "0.050")],
                call_provider=_caller(log),
                quality_gate=_gate_from_outcomes({"cheap": False, "strong": False}, log=log),
            )
        )

    assert exc_info.value.attempted_provider_ids == ("cheap", "strong")
    assert exc_info.value.total_cost_usd == Decimal("0.060")
    assert [attempt.gate.reason for attempt in exc_info.value.attempts] == [
        "answer:cheap below confidence floor",
        "answer:strong below confidence floor",
    ]
    assert log.called == ["cheap", "strong"]


@pytest.mark.anyio
async def test_actual_cost_callable_overrides_tier_estimate_per_attempt() -> None:
    log = AttemptLog()

    result = await route_with_cascade(
        CascadeProviderRequest(
            tiers=[_tier("cheap", "0.010"), _tier("strong", "0.050")],
            call_provider=_caller(log),
            quality_gate=_gate_from_outcomes({"cheap": False, "strong": True}, log=log),
            cost_of_result=lambda provider, value: (
                Decimal("0.001") if provider.id == "cheap" else Decimal("0.007")
            ),
        )
    )

    assert [attempt.cost_usd for attempt in result.attempts] == [
        Decimal("0.001"),
        Decimal("0.007"),
    ]
    assert result.total_cost_usd == Decimal("0.008")


@pytest.mark.anyio
async def test_actual_cost_callable_rejects_float_cost_usd() -> None:
    log = AttemptLog()

    with pytest.raises(ValueError, match="cost_of_result must be a Decimal"):
        await route_with_cascade(
            CascadeProviderRequest(
                tiers=[_tier("cheap", "0.010")],
                call_provider=_caller(log),
                quality_gate=_gate_from_outcomes({"cheap": True}, log=log),
                cost_of_result=lambda provider, value: 0.1,
            )
        )


@pytest.mark.anyio
async def test_max_attempts_caps_escalation_chain() -> None:
    log = AttemptLog()

    with pytest.raises(CascadeRoutingError) as exc_info:
        await route_with_cascade(
            CascadeProviderRequest(
                tiers=[
                    _tier("cheap", "0.010"),
                    _tier("middle", "0.025"),
                    _tier("strong", "0.050"),
                ],
                call_provider=_caller(log),
                quality_gate=_gate_from_outcomes(
                    {"cheap": False, "middle": False, "strong": True},
                    log=log,
                ),
                max_attempts=2,
            )
        )

    assert exc_info.value.attempted_provider_ids == ("cheap", "middle")
    assert log.called == ["cheap", "middle"]


@pytest.mark.anyio
async def test_sync_gate_callable_is_supported_for_deterministic_fake_gates() -> None:
    log = AttemptLog()

    def gate(provider: FakeProvider, value: str) -> CascadeGateDecision:
        log.gated.append(provider.id)
        return CascadeGateDecision(passed=value.endswith("strong"), confidence=0.8)

    result = await route_with_cascade(
        CascadeProviderRequest(
            tiers=[_tier("cheap", "0.010"), _tier("strong", "0.050")],
            call_provider=_caller(log),
            quality_gate=gate,
        )
    )

    assert result.provider_id == "strong"
    assert result.attempted_provider_ids == ("cheap", "strong")
    assert log.gated == ["cheap", "strong"]


@pytest.mark.anyio
async def test_mapping_provider_ids_are_supported_by_default() -> None:
    tiers: list[CascadeTier[dict[str, str]]] = [
        CascadeTier(provider={"id": "cheap"}, estimated_cost_usd=Decimal("0.010")),
    ]

    async def call(provider: dict[str, str]) -> str:
        return provider["id"]

    def gate(provider: dict[str, str], value: str) -> CascadeGateDecision:
        return CascadeGateDecision(passed=value == "cheap")

    result = await route_with_cascade(
        CascadeProviderRequest(
            tiers=tiers,
            call_provider=call,
            quality_gate=gate,
        )
    )

    assert result.provider_id == "cheap"
    assert result.total_cost_usd == Decimal("0.010")


@pytest.mark.anyio
async def test_validation_rejects_invalid_cascade_requests() -> None:
    valid_tiers = [_tier("cheap", "0.010")]
    call_provider = _caller(AttemptLog())
    quality_gate = _gate_from_outcomes({"cheap": True}, log=AttemptLog())

    invalid_requests = [
        CascadeProviderRequest(
            tiers=[],
            call_provider=call_provider,
            quality_gate=quality_gate,
        ),
        CascadeProviderRequest(
            tiers=valid_tiers,
            call_provider=call_provider,
            quality_gate=quality_gate,
            max_attempts=0,
        ),
        CascadeProviderRequest(
            tiers=[_tier("strong", "0.050"), _tier("cheap", "0.010")],
            call_provider=call_provider,
            quality_gate=quality_gate,
        ),
        CascadeProviderRequest(
            tiers=[_tier("dup", "0.010"), _tier("dup", "0.020")],
            call_provider=call_provider,
            quality_gate=quality_gate,
        ),
    ]

    for request in invalid_requests:
        with pytest.raises(ValueError):
            await route_with_cascade(request)

    with pytest.raises(ValueError, match="confidence"):
        CascadeGateDecision(passed=True, confidence=1.5)
