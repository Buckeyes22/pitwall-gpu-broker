"""Lease lifecycle transition guard.

Service code should use :func:`transition_lease_state` for all persisted lease
state changes so invalid jumps fail before they reach storage.
"""

from __future__ import annotations

from collections.abc import Mapping

from pitwall.core.enums import LeaseState

LeaseStateInput = LeaseState | str


class LeaseTransitionError(RuntimeError):
    """Base class for lease transition failures."""

    error_code = "lease_transition_error"


class IllegalLeaseTransitionError(LeaseTransitionError):
    """Raised when a lease lifecycle transition is not allowed."""

    error_code = "illegal_lease_transition"

    def __init__(self, from_state: LeaseState, to_state: LeaseState) -> None:
        message = f"illegal lease state transition: {from_state.value} -> {to_state.value}"
        super().__init__(message)
        self.from_state = from_state
        self.to_state = to_state

    def to_dict(self) -> dict[str, str]:
        return {
            "error": self.error_code,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
        }


LEASE_STATE_TRANSITIONS: Mapping[LeaseState, frozenset[LeaseState]] = {
    LeaseState.CREATING: frozenset({LeaseState.WAITING_RUNTIME, LeaseState.FAILED}),
    LeaseState.WAITING_RUNTIME: frozenset({LeaseState.WAITING_PROBE, LeaseState.FAILED}),
    LeaseState.WAITING_PROBE: frozenset({LeaseState.ACTIVE, LeaseState.FAILED}),
    LeaseState.ACTIVE: frozenset({LeaseState.STOPPING, LeaseState.EXPIRED, LeaseState.FAILED}),
    LeaseState.STOPPING: frozenset({LeaseState.STOPPED, LeaseState.EXPIRED, LeaseState.FAILED}),
    LeaseState.STOPPED: frozenset(),
    LeaseState.FAILED: frozenset(),
    LeaseState.EXPIRED: frozenset(),
}

TERMINAL_LEASE_STATES = frozenset({LeaseState.STOPPED, LeaseState.FAILED, LeaseState.EXPIRED})
ACTIVE_LEASE_STATES = frozenset(
    {
        LeaseState.CREATING,
        LeaseState.WAITING_RUNTIME,
        LeaseState.WAITING_PROBE,
        LeaseState.ACTIVE,
        LeaseState.STOPPING,
    }
)
VALID_LEASE_STATE_TRANSITIONS = LEASE_STATE_TRANSITIONS
InvalidLeaseTransitionError = IllegalLeaseTransitionError


def transition_lease_state(
    from_state: LeaseStateInput,
    to_state: LeaseStateInput,
) -> LeaseState:
    """Validate and return the next lease state.

    The returned enum is safe to persist. Illegal lifecycle jumps raise
    :class:`IllegalLeaseTransitionError`.
    """

    current = _coerce_lease_state(from_state, field_name="from_state")
    target = _coerce_lease_state(to_state, field_name="to_state")
    if target not in LEASE_STATE_TRANSITIONS[current]:
        raise IllegalLeaseTransitionError(current, target)
    return target


def can_transition_lease(
    from_state: LeaseStateInput,
    to_state: LeaseStateInput,
) -> bool:
    """Return whether ``from_state`` may legally move to ``to_state``."""

    current = _coerce_lease_state(from_state, field_name="from_state")
    target = _coerce_lease_state(to_state, field_name="to_state")
    return target in LEASE_STATE_TRANSITIONS[current]


def _coerce_lease_state(value: LeaseStateInput, *, field_name: str) -> LeaseState:
    if isinstance(value, LeaseState):
        return value
    if isinstance(value, str):
        try:
            return LeaseState(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} is not a valid lease state: {value!r}") from exc
    raise TypeError(f"{field_name} must be a LeaseState or string")


transition = transition_lease_state
can_transition = can_transition_lease


__all__ = [
    "ACTIVE_LEASE_STATES",
    "LEASE_STATE_TRANSITIONS",
    "TERMINAL_LEASE_STATES",
    "IllegalLeaseTransitionError",
    "InvalidLeaseTransitionError",
    "LeaseTransitionError",
    "VALID_LEASE_STATE_TRANSITIONS",
    "can_transition",
    "can_transition_lease",
    "transition",
    "transition_lease_state",
]
