from __future__ import annotations

import pytest

from pitwall.core.enums import LeaseState
from pitwall.leases.state import (
    IllegalLeaseTransitionError,
    can_transition_lease,
    transition_lease_state,
)

_EXPECTED_ALLOWED: dict[LeaseState, frozenset[LeaseState]] = {
    LeaseState.CREATING: frozenset({LeaseState.WAITING_RUNTIME, LeaseState.FAILED}),
    LeaseState.WAITING_RUNTIME: frozenset({LeaseState.WAITING_PROBE, LeaseState.FAILED}),
    LeaseState.WAITING_PROBE: frozenset({LeaseState.ACTIVE, LeaseState.FAILED}),
    LeaseState.ACTIVE: frozenset({LeaseState.STOPPING, LeaseState.EXPIRED, LeaseState.FAILED}),
    LeaseState.STOPPING: frozenset({LeaseState.STOPPED, LeaseState.EXPIRED, LeaseState.FAILED}),
    LeaseState.STOPPED: frozenset(),
    LeaseState.FAILED: frozenset(),
    LeaseState.EXPIRED: frozenset(),
}

_ALL_PAIRS = tuple((current, target) for current in LeaseState for target in LeaseState)


def test_expected_matrix_covers_every_lease_state() -> None:
    assert set(_EXPECTED_ALLOWED) == set(LeaseState)
    for allowed_targets in _EXPECTED_ALLOWED.values():
        assert allowed_targets <= set(LeaseState)


@pytest.mark.parametrize(
    ("current", "target"),
    _ALL_PAIRS,
    ids=[f"{current.value}->{target.value}" for current, target in _ALL_PAIRS],
)
def test_full_lease_state_transition_matrix(
    current: LeaseState,
    target: LeaseState,
) -> None:
    if target in _EXPECTED_ALLOWED[current]:
        assert can_transition_lease(current, target) is True
        assert transition_lease_state(current, target) is target
        assert transition_lease_state(current.value, target.value) is target
        return

    assert can_transition_lease(current, target) is False
    assert can_transition_lease(current.value, target.value) is False
    with pytest.raises(IllegalLeaseTransitionError) as exc_info:
        transition_lease_state(current, target)

    assert exc_info.value.from_state is current
    assert exc_info.value.to_state is target
