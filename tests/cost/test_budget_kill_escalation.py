"""Tests for budget-breach -> kill-switch escalation.

Covers the three modes (disabled/shadow/armed), the trigger gate (only on a
hard ``block`` with exhausted headroom), and that nothing fires unless armed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from pitwall.cost.budget_kill_escalation import (
    KillEscalationPolicy,
    evaluate_kill_escalation,
    maybe_escalate_to_kill,
)
from pitwall.cost.circuit_breaker import BreakerAction, CircuitBreakerDecision

pytestmark = pytest.mark.anyio


def _decision(action: BreakerAction, headroom_usd: str) -> CircuitBreakerDecision:
    return CircuitBreakerDecision(
        action=action,
        reason=f"{action} @ {headroom_usd}",
        state="open" if action == "block" else "closed",
        headroom_usd=Decimal(headroom_usd),
        headroom_pct=Decimal("0"),
        runway_hours=Decimal("0"),
    )


class _FakeKillSwitch:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def activate(self, reason: str) -> dict[str, Any]:
        self.calls.append(reason)
        return {"fired": True, "reason": reason}


# ---- pure decision ----------------------------------------------------------


def test_disabled_never_fires() -> None:
    d = evaluate_kill_escalation(_decision("block", "-5"), mode="disabled")
    assert d.should_fire is False


def test_block_with_exhausted_headroom_triggers() -> None:
    d = evaluate_kill_escalation(_decision("block", "0"), mode="armed")
    assert d.should_fire is True


def test_block_with_remaining_headroom_does_not_trigger() -> None:
    # block but still $5 headroom (above the default 0 floor) -> no escalation
    d = evaluate_kill_escalation(_decision("block", "5"), mode="armed")
    assert d.should_fire is False


@pytest.mark.parametrize("action", ["allow", "downgrade"])
def test_non_block_actions_never_trigger(action: str) -> None:
    d = evaluate_kill_escalation(_decision(action, "-100"), mode="armed")
    assert d.should_fire is False


def test_relaxed_require_block_allows_downgrade_trigger() -> None:
    policy = KillEscalationPolicy(require_block=False)
    d = evaluate_kill_escalation(_decision("downgrade", "-1"), mode="armed", policy=policy)
    assert d.should_fire is True


def test_custom_headroom_floor() -> None:
    policy = KillEscalationPolicy(headroom_floor_usd=Decimal("10"))
    assert evaluate_kill_escalation(
        _decision("block", "8"), mode="armed", policy=policy
    ).should_fire
    assert not evaluate_kill_escalation(
        _decision("block", "12"), mode="armed", policy=policy
    ).should_fire


# ---- async invoker (the only thing that can fire) ---------------------------


async def test_disabled_invoker_is_inert() -> None:
    ks = _FakeKillSwitch()
    out = await maybe_escalate_to_kill(_decision("block", "-50"), ks, mode="disabled")
    assert out.fired is False
    assert ks.calls == []


async def test_shadow_logs_but_never_fires() -> None:
    ks = _FakeKillSwitch()
    out = await maybe_escalate_to_kill(_decision("block", "0"), ks, mode="shadow")
    assert out.fired is False
    assert out.mode == "shadow"
    assert "SHADOW" in out.reason
    assert ks.calls == []  # critical: shadow must NOT touch the kill switch


async def test_armed_fires_on_breach() -> None:
    ks = _FakeKillSwitch()
    out = await maybe_escalate_to_kill(_decision("block", "-1"), ks, mode="armed")
    assert out.fired is True
    assert out.report == {"fired": True, "reason": ks.calls[0]}
    assert len(ks.calls) == 1
    assert "budget-breach auto-kill" in ks.calls[0]


@pytest.mark.parametrize("bad_mode", ["", "armd", "enabled", "on", "ARMED", "Disabled"])
async def test_invalid_mode_canonicalizes_to_disabled_and_never_fires(
    bad_mode: str,
) -> None:
    # A malformed mode (config typo) must NEVER fall through to firing the switch.
    ks = _FakeKillSwitch()
    out = await maybe_escalate_to_kill(
        _decision("block", "-999"),
        ks,
        mode=bad_mode,
    )
    assert out.fired is False
    assert out.mode == "disabled"
    assert bad_mode in out.reason
    assert ks.calls == []
    decision = evaluate_kill_escalation(
        _decision("block", "-999"),
        mode=bad_mode,
    )
    assert decision.should_fire is False
    assert decision.mode == "disabled"
    assert bad_mode in decision.reason


async def test_armed_does_not_fire_without_trigger() -> None:
    ks = _FakeKillSwitch()
    out = await maybe_escalate_to_kill(_decision("downgrade", "-1"), ks, mode="armed")
    assert out.fired is False
    assert ks.calls == []
