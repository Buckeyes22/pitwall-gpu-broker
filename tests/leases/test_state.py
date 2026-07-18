from __future__ import annotations

import pytest

from pitwall.core.enums import LeaseState
from pitwall.leases import (
    TERMINAL_LEASE_STATES,
    IllegalLeaseTransitionError,
    can_transition_lease,
    transition_lease_state,
)


def test_happy_path_transitions_return_target_state() -> None:
    state = transition_lease_state("creating", "waiting_runtime")
    state = transition_lease_state(state, LeaseState.WAITING_PROBE)
    state = transition_lease_state(state, LeaseState.ACTIVE)
    state = transition_lease_state(state, LeaseState.STOPPING)
    state = transition_lease_state(state, LeaseState.STOPPED)

    assert state is LeaseState.STOPPED


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (LeaseState.CREATING, LeaseState.ACTIVE),
        (LeaseState.WAITING_RUNTIME, LeaseState.STOPPING),
        (LeaseState.ACTIVE, LeaseState.STOPPED),
        (LeaseState.STOPPED, LeaseState.ACTIVE),
        (LeaseState.FAILED, LeaseState.CREATING),
        (LeaseState.EXPIRED, LeaseState.STOPPING),
    ],
)
def test_illegal_transitions_raise_typed_error(
    from_state: LeaseState,
    to_state: LeaseState,
) -> None:
    with pytest.raises(IllegalLeaseTransitionError) as exc_info:
        transition_lease_state(from_state, to_state)

    assert exc_info.value.from_state is from_state
    assert exc_info.value.to_state is to_state


def test_illegal_transition_error_message_and_payload() -> None:
    error = IllegalLeaseTransitionError(LeaseState.ACTIVE, LeaseState.STOPPED)

    assert str(error) == "illegal lease state transition: active -> stopped"
    assert error.to_dict() == {
        "error": "illegal_lease_transition",
        "from_state": "active",
        "to_state": "stopped",
    }


def test_terminal_states_have_no_outbound_transitions() -> None:
    for terminal_state in TERMINAL_LEASE_STATES:
        assert not can_transition_lease(terminal_state, LeaseState.STOPPING)


def test_failure_and_expiry_terminal_paths_are_guarded() -> None:
    assert transition_lease_state(LeaseState.CREATING, LeaseState.FAILED) is LeaseState.FAILED
    assert transition_lease_state(LeaseState.ACTIVE, LeaseState.EXPIRED) is LeaseState.EXPIRED
    assert transition_lease_state(LeaseState.STOPPING, LeaseState.EXPIRED) is LeaseState.EXPIRED


def test_unknown_state_input_is_rejected_before_transition_check() -> None:
    with pytest.raises(
        ValueError,
        match="from_state is not a valid lease state: 'launching'",
    ):
        transition_lease_state("launching", "active")


def test_unknown_target_state_mentions_to_state() -> None:
    with pytest.raises(
        ValueError,
        match="to_state is not a valid lease state: 'launching'",
    ):
        transition_lease_state("active", "launching")


def test_can_transition_rejects_unknown_states_with_field_names() -> None:
    with pytest.raises(
        ValueError,
        match="from_state is not a valid lease state: 'launching'",
    ):
        can_transition_lease("launching", "active")
    with pytest.raises(
        ValueError,
        match="to_state is not a valid lease state: 'launching'",
    ):
        can_transition_lease("active", "launching")


def test_non_string_state_inputs_raise_type_errors_with_field_names() -> None:
    with pytest.raises(TypeError, match="from_state must be a LeaseState or string"):
        transition_lease_state(object(), "active")  # type: ignore[arg-type]  # reason: intentionally wrong type to exercise validation
    with pytest.raises(TypeError, match="to_state must be a LeaseState or string"):
        transition_lease_state("active", object())  # type: ignore[arg-type]  # reason: intentionally wrong type to exercise validation
    with pytest.raises(TypeError, match="from_state must be a LeaseState or string"):
        can_transition_lease(object(), "active")  # type: ignore[arg-type]  # reason: intentionally wrong type to exercise validation
    with pytest.raises(TypeError, match="to_state must be a LeaseState or string"):
        can_transition_lease("active", object())  # type: ignore[arg-type]  # reason: intentionally wrong type to exercise validation
