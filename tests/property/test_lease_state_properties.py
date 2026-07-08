"""Property-based + exhaustive tests for the lease state machine.

Grounded in src/pitwall/leases/state.py (verified 2026-05-30): 8 states
(creating, waiting_runtime, waiting_probe, active, stopping, stopped, failed,
expired); `TERMINAL_LEASE_STATES = {stopped, failed, expired}` is exported;
`transition_lease_state(f, t)` returns `t` on a legal edge and raises
`IllegalLeaseTransitionError` otherwise; `can_transition_lease(f, t)` is the
non-raising predicate. Both accept enum or string inputs.

Invariants:
    1. transition_lease_state(f,t) returns t iff t in table[f], else raises (exhaustive 8x8)
    2. can_transition_lease agrees with the table and never raises
    3. can_transition_lease(f,t) is True iff transition_lease_state(f,t) doesn't raise
    4. terminal states have no outgoing edges and reject every transition
    5. table is closed over the enum (keys == LeaseState; every target is a LeaseState)
    6. every non-terminal state can reach a terminal state (no live-lock)
    7. string and enum inputs behave identically
"""

from __future__ import annotations

from collections import deque

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.core.enums import LeaseState
from pitwall.leases.state import (
    LEASE_STATE_TRANSITIONS,
    TERMINAL_LEASE_STATES,
    IllegalLeaseTransitionError,
    can_transition_lease,
    transition_lease_state,
)

pytestmark = pytest.mark.property

_ALL = list(LeaseState)
_PAIRS = [(f, t) for f in _ALL for t in _ALL]
_NON_TERMINAL = [s for s in _ALL if s not in TERMINAL_LEASE_STATES]
_lease_states = st.sampled_from(_ALL)


def test_terminal_set_matches_empty_table_entries() -> None:
    """The exported terminal set must be exactly the states with no outgoing edges."""
    derived = {s for s in _ALL if not LEASE_STATE_TRANSITIONS[s]}
    assert set(TERMINAL_LEASE_STATES) == derived
    assert TERMINAL_LEASE_STATES  # non-empty


# --- Property 1: legal iff in table (exhaustive 8x8) ----------------------
@pytest.mark.parametrize(
    "from_state,to_state",
    _PAIRS,
    ids=[f"{f.value}->{t.value}" for f, t in _PAIRS],
)
def test_transition_legal_iff_in_table(from_state: LeaseState, to_state: LeaseState) -> None:
    allowed = to_state in LEASE_STATE_TRANSITIONS[from_state]
    if allowed:
        assert transition_lease_state(from_state, to_state) is to_state
    else:
        with pytest.raises(IllegalLeaseTransitionError):
            transition_lease_state(from_state, to_state)


# --- Property 2 + 3: can_transition agrees with table, never raises -------
@given(f=_lease_states, t=_lease_states)
def test_can_transition_matches_table_and_never_raises(f: LeaseState, t: LeaseState) -> None:
    expected = t in LEASE_STATE_TRANSITIONS[f]
    assert can_transition_lease(f, t) is expected
    raised = False
    try:
        transition_lease_state(f, t)
    except IllegalLeaseTransitionError:
        raised = True
    assert can_transition_lease(f, t) is (not raised)


# --- Property 4: terminal states are sinks --------------------------------
@pytest.mark.parametrize(
    "term", sorted(TERMINAL_LEASE_STATES, key=lambda s: s.value), ids=lambda s: s.value
)
def test_terminal_states_have_no_outgoing(term: LeaseState) -> None:
    assert LEASE_STATE_TRANSITIONS[term] == frozenset()
    for t in _ALL:
        with pytest.raises(IllegalLeaseTransitionError):
            transition_lease_state(term, t)


# --- Property 5: table closure (no target escapes the enum) ---------------
def test_table_is_closed_over_enum() -> None:
    assert set(LEASE_STATE_TRANSITIONS.keys()) == set(_ALL)
    for targets in LEASE_STATE_TRANSITIONS.values():
        assert targets <= set(_ALL)


# --- Property 6: every non-terminal state can reach a terminal state -------
def _reaches_terminal(start: LeaseState) -> bool:
    seen: set[LeaseState] = {start}
    queue: deque[LeaseState] = deque([start])
    while queue:
        cur = queue.popleft()
        if cur in TERMINAL_LEASE_STATES:
            return True
        for nxt in LEASE_STATE_TRANSITIONS[cur]:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


@pytest.mark.parametrize("state", _NON_TERMINAL, ids=lambda s: s.value)
def test_every_non_terminal_reaches_terminal(state: LeaseState) -> None:
    assert _reaches_terminal(state)


@given(start=_lease_states)
def test_bounded_walk_eventually_terminates(start: LeaseState) -> None:
    # Step to the sorted-first legal target each iteration. Every non-terminal
    # state has a `failed` edge, so a terminal is always reachable within
    # len(states) steps.
    cur = start
    for _ in range(len(_ALL) + 1):
        if cur in TERMINAL_LEASE_STATES:
            break
        targets = sorted(LEASE_STATE_TRANSITIONS[cur], key=lambda s: s.value)
        assert targets, f"non-terminal {cur} unexpectedly has no targets"
        cur = transition_lease_state(cur, targets[0])
    assert cur in TERMINAL_LEASE_STATES


# --- Property 7: string and enum inputs are equivalent --------------------
@given(f=_lease_states, t=_lease_states)
def test_string_and_enum_inputs_equivalent(f: LeaseState, t: LeaseState) -> None:
    assert can_transition_lease(f.value, t.value) == can_transition_lease(f, t)
    if can_transition_lease(f, t):
        assert transition_lease_state(f.value, t.value) is t
    else:
        with pytest.raises(IllegalLeaseTransitionError):
            transition_lease_state(f.value, t.value)
