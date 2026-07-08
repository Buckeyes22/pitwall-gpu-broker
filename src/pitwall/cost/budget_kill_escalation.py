"""Budget-breach → kill-switch escalation (opt-in, three-mode, deterministic).

The budget enforcement ladder is, in increasing severity:

    per-run cap reject → monthly cap reject → circuit-breaker downgrade →
    circuit-breaker block → **budget-breach kill escalation** (this module)

Auto-terminating *running* compute on a budget number is dangerous, so this
escalation is **disabled by default** and gated three ways:

* ``mode`` (``PITWALL_BUDGET_BREACH_KILL_MODE``):
    - ``disabled`` (default): never escalates; this module is inert.
    - ``shadow``: evaluates the trigger and LOGS what it *would* do, but never
      fires the kill switch — lets operators validate the trigger before arming.
    - ``armed``: actually fires the kill switch when the trigger condition holds.
* It only escalates on a hard circuit-breaker ``block`` decision (never on
  ``allow``/``downgrade``) — unless ``require_block`` is explicitly relaxed.
* It only escalates once headroom has fallen to/below ``headroom_floor_usd``
  (default ``0`` — i.e. the budget is fully exhausted/overrun), not merely low.

The decision is pure and deterministic; the invoker is the only async part and
the only thing that can fire the switch. The kill switch is injected via a
minimal Protocol so this module stays decoupled from ``pitwall.api.admin``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

from pitwall.cost.circuit_breaker import CircuitBreakerDecision

log = logging.getLogger("pitwall.cost.budget_kill_escalation")

type KillEscalationMode = Literal["disabled", "shadow", "armed"]


class KillSwitchLike(Protocol):
    """Anything that can be activated with a reason — e.g. ``CloudKillSwitch``."""

    async def activate(self, reason: str) -> Any: ...


@dataclass(frozen=True)
class KillEscalationPolicy:
    """When a budget breach should escalate to the kill switch.

    Attributes:
        require_block: Only escalate on a circuit-breaker ``block`` action
            (never ``allow``/``downgrade``). Relax at your own risk.
        headroom_floor_usd: Only escalate when budget headroom is at or below
            this floor. Default ``0`` => only when the budget is fully exhausted.
    """

    require_block: bool = True
    headroom_floor_usd: Decimal = Decimal("0")


@dataclass(frozen=True)
class KillEscalationDecision:
    """Pure decision: should the kill switch fire, given a breaker decision."""

    should_fire: bool
    mode: KillEscalationMode
    reason: str


@dataclass(frozen=True)
class KillEscalationOutcome:
    """Result of an escalation attempt.

    ``fired`` is True only when the switch was actually activated (mode=armed
    and the trigger held). In ``shadow`` mode ``fired`` is always False even
    when the trigger would have held (see ``reason``).
    """

    fired: bool
    mode: KillEscalationMode
    reason: str
    report: Any | None = None


def evaluate_kill_escalation(
    decision: CircuitBreakerDecision,
    *,
    mode: str,
    policy: KillEscalationPolicy | None = None,
) -> KillEscalationDecision:
    """Decide whether a breaker decision warrants kill escalation. Pure/deterministic."""
    policy = policy or KillEscalationPolicy()
    canonical_mode, invalid_reason = _normalize_mode(mode)
    if invalid_reason is not None:
        return KillEscalationDecision(False, canonical_mode, invalid_reason)
    if canonical_mode == "disabled":
        return KillEscalationDecision(
            False,
            canonical_mode,
            "budget-breach kill escalation inert (mode='disabled')",
        )
    block_ok = decision.action == "block" or not policy.require_block
    headroom_breached = decision.headroom_usd <= policy.headroom_floor_usd
    if not (block_ok and headroom_breached):
        return KillEscalationDecision(
            False,
            canonical_mode,
            f"no escalation (action={decision.action}, "
            f"headroom={decision.headroom_usd} vs floor={policy.headroom_floor_usd})",
        )
    return KillEscalationDecision(
        True,
        canonical_mode,
        f"budget breach: action={decision.action}, headroom {decision.headroom_usd} "
        f"<= floor {policy.headroom_floor_usd} ({decision.reason})",
    )


async def maybe_escalate_to_kill(
    decision: CircuitBreakerDecision,
    kill_switch: KillSwitchLike,
    *,
    mode: str,
    policy: KillEscalationPolicy | None = None,
) -> KillEscalationOutcome:
    """Evaluate and, in ``armed`` mode only, fire the kill switch on a budget breach.

    ``disabled`` => inert. ``shadow`` => logs intent, never fires. ``armed`` =>
    fires ``kill_switch.activate(...)`` when the trigger holds. Returns a
    structured outcome for the audit trail in all cases.
    """
    decided = evaluate_kill_escalation(decision, mode=mode, policy=policy)
    if not decided.should_fire:
        return KillEscalationOutcome(False, decided.mode, decided.reason)
    if decided.mode != "armed":
        # shadow mode logs intent, but never fires.
        log.warning("SHADOW budget-breach kill escalation WOULD fire: %s", decided.reason)
        return KillEscalationOutcome(False, decided.mode, f"SHADOW (no-fire): {decided.reason}")
    log.critical("ARMED budget-breach kill escalation FIRING kill switch: %s", decided.reason)
    report = await kill_switch.activate(f"budget-breach auto-kill: {decided.reason}")
    return KillEscalationOutcome(True, "armed", decided.reason, report)


def _normalize_mode(mode: str) -> tuple[KillEscalationMode, str | None]:
    if mode == "disabled":
        return "disabled", None
    if mode == "shadow":
        return "shadow", None
    if mode == "armed":
        return "armed", None
    return (
        "disabled",
        f"invalid budget-breach kill mode {mode!r}; fail-closed to mode='disabled'",
    )


__all__ = [
    "KillEscalationDecision",
    "KillEscalationMode",
    "KillEscalationOutcome",
    "KillEscalationPolicy",
    "KillSwitchLike",
    "evaluate_kill_escalation",
    "maybe_escalate_to_kill",
]
