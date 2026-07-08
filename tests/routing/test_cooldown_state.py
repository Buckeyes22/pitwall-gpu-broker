"""Tests for provider cooldown state transitions — ."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pitwall.routing import (
    DEFAULT_ESCALATED_COOLDOWN,
    DEFAULT_INITIAL_COOLDOWN,
    CooldownPolicy,
    CooldownState,
    CooldownStateMachine,
    apply_probe_result,
    cooldown_duration_for_trip,
    is_in_cooldown,
    record_provider_failure,
    record_provider_success,
    state_from_provider,
    to_provider_patch,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def test_three_consecutive_failures_enter_five_minute_cooldown() -> None:
    state = CooldownState(health_status="healthy")

    first = record_provider_failure(state, now=_NOW)
    second = record_provider_failure(first, now=_NOW + timedelta(seconds=1))
    third = record_provider_failure(second, now=_NOW + timedelta(seconds=2))

    assert first.consecutive_failures == 1
    assert first.cooldown_until is None
    assert second.consecutive_failures == 2
    assert second.cooldown_until is None
    assert third.consecutive_failures == 3
    assert third.cooldown_trips == 1
    assert third.cooldown_until == _NOW + timedelta(seconds=2) + DEFAULT_INITIAL_COOLDOWN
    assert third.health_status == "unhealthy"


def test_repeated_trip_escalates_to_fifteen_minutes() -> None:
    state = CooldownState()
    for offset_s in range(3):
        state = record_provider_failure(state, now=_NOW + timedelta(seconds=offset_s))

    after_first_cooldown = state.cooldown_until + timedelta(seconds=1)
    state = record_provider_failure(state, now=after_first_cooldown)
    state = record_provider_failure(state, now=after_first_cooldown + timedelta(seconds=1))
    state = record_provider_failure(state, now=after_first_cooldown + timedelta(seconds=2))

    assert state.consecutive_failures == 6
    assert state.cooldown_trips == 2
    assert state.cooldown_until == (
        after_first_cooldown + timedelta(seconds=2) + DEFAULT_ESCALATED_COOLDOWN
    )


def test_success_resets_failure_epoch_and_clears_cooldown() -> None:
    cooling = CooldownState(
        consecutive_failures=3,
        cooldown_trips=1,
        cooldown_until=_NOW + timedelta(minutes=5),
        health_status="unhealthy",
    )

    state = record_provider_success(cooling, now=_NOW + timedelta(seconds=10))

    assert state == CooldownState(health_status="healthy")


def test_active_cooldown_does_not_extend_on_additional_failure() -> None:
    cooling = CooldownState(
        consecutive_failures=3,
        cooldown_trips=1,
        cooldown_until=_NOW + timedelta(minutes=5),
        health_status="unhealthy",
    )

    state = record_provider_failure(cooling, now=_NOW + timedelta(minutes=1))

    assert state == cooling


def test_provider_mapping_round_trips_to_repository_patch() -> None:
    provider = {
        "consecutive_failures": 2,
        "cooldown_trips": 0,
        "cooldown_until": None,
        "health_status": "healthy",
    }

    state = apply_probe_result(provider, passed=False, now=_NOW)

    assert state.consecutive_failures == 3
    assert to_provider_patch(state) == {
        "consecutive_failures": 3,
        "cooldown_trips": 1,
        "cooldown_until": _NOW + DEFAULT_INITIAL_COOLDOWN,
        "health_status": "unhealthy",
    }


def test_cooldown_window_is_time_bounded() -> None:
    state = CooldownState(cooldown_until=_NOW + timedelta(seconds=30))

    assert is_in_cooldown(state, now=_NOW) is True
    assert is_in_cooldown(state, now=_NOW + timedelta(seconds=30)) is False


def test_state_from_provider_accepts_iso_cooldown_timestamp() -> None:
    state = state_from_provider(
        {
            "consecutive_failures": "3",
            "cooldown_trips": "1",
            "cooldown_until": "2026-05-28T12:05:00Z",
            "health_status": "unhealthy",
        }
    )

    assert state.cooldown_until == datetime(2026, 5, 28, 12, 5, tzinfo=UTC)


def test_policy_validation_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        record_provider_failure(CooldownState(), now=_NOW, failure_threshold=0)

    with pytest.raises(ValueError, match="cooldown_trip"):
        cooldown_duration_for_trip(0)

    with pytest.raises(ValueError, match="now"):
        record_provider_failure(CooldownState(), now=datetime(2026, 5, 28, 12, 0, 0))


def test_state_machine_wrapper_uses_custom_policy() -> None:
    machine = CooldownStateMachine(
        CooldownPolicy(
            failure_threshold=2,
            initial_cooldown=timedelta(minutes=1),
            escalated_cooldown=timedelta(minutes=2),
        )
    )

    state = machine.record_failure(CooldownState(), now=_NOW)
    state = machine.record_failure(state, now=_NOW + timedelta(seconds=1))

    assert state.cooldown_trips == 1
    assert state.cooldown_until == _NOW + timedelta(seconds=1, minutes=1)


def test_health_status_is_healthy_until_cooldown_trips() -> None:
    state = CooldownState(health_status="healthy")

    first = record_provider_failure(state, now=_NOW)
    second = record_provider_failure(first, now=_NOW + timedelta(seconds=1))

    assert first.health_status == "healthy"
    assert second.health_status == "healthy"


def test_recovery_after_cooldown_expires_then_probe_succeeds() -> None:
    state = CooldownState(
        consecutive_failures=3,
        cooldown_trips=1,
        cooldown_until=_NOW + timedelta(minutes=5),
        health_status="unhealthy",
    )

    expired_time = _NOW + timedelta(minutes=5, seconds=1)
    assert is_in_cooldown(state, now=expired_time) is False

    recovered = record_provider_success(state, now=expired_time)

    assert recovered.health_status == "healthy"
    assert recovered.consecutive_failures == 0
    assert recovered.cooldown_trips == 0
    assert recovered.cooldown_until is None


def test_provider_stays_unhealthy_after_cooldown_expires_without_success() -> None:
    state = CooldownState(
        consecutive_failures=3,
        cooldown_trips=1,
        cooldown_until=_NOW + timedelta(minutes=5),
        health_status="unhealthy",
    )

    expired_time = _NOW + timedelta(minutes=5, seconds=1)
    assert is_in_cooldown(state, now=expired_time) is False

    still_unhealthy = record_provider_failure(state, now=expired_time)

    assert still_unhealthy.health_status == "unhealthy"
    assert still_unhealthy.consecutive_failures == 4
    assert still_unhealthy.cooldown_trips == 1
    assert is_in_cooldown(still_unhealthy, now=expired_time) is False


def test_multiple_cooldown_trips_require_repeated_failures_after_each_cooldown() -> None:
    state = CooldownState()
    for offset_s in range(3):
        state = record_provider_failure(state, now=_NOW + timedelta(seconds=offset_s))

    assert state.health_status == "unhealthy"
    assert state.cooldown_trips == 1

    after_first_cooldown = state.cooldown_until + timedelta(seconds=1)
    for offset_s in range(3):
        state = record_provider_failure(
            state, now=after_first_cooldown + timedelta(seconds=offset_s)
        )

    assert state.cooldown_trips == 2
    assert state.health_status == "unhealthy"


def test_success_clears_cooldown_and_resets_health_to_healthy() -> None:
    state = CooldownState(
        consecutive_failures=6,
        cooldown_trips=2,
        cooldown_until=_NOW + timedelta(minutes=15),
        health_status="unhealthy",
    )

    recovered = record_provider_success(state, now=_NOW + timedelta(minutes=16))

    assert recovered.health_status == "healthy"
    assert recovered.consecutive_failures == 0
    assert recovered.cooldown_trips == 0
    assert recovered.cooldown_until is None


def test_record_failure_during_cooldown_returns_current_state() -> None:
    cooling = CooldownState(
        consecutive_failures=3,
        cooldown_trips=1,
        cooldown_until=_NOW + timedelta(minutes=5),
        health_status="unhealthy",
    )

    during_cooldown = _NOW + timedelta(minutes=1)
    result = record_provider_failure(cooling, now=during_cooldown)

    assert result == cooling
    assert result.health_status == "unhealthy"


def test_unknown_health_status_defaults_to_unknown() -> None:
    state = CooldownState()

    assert state.health_status == "unknown"


def test_provider_without_cooldown_fields_maps_correctly() -> None:
    provider = {
        "id": "prov1",
        "health_status": "healthy",
    }

    state = state_from_provider(provider)

    assert state.health_status == "healthy"
    assert state.consecutive_failures == 0
    assert state.cooldown_trips == 0
    assert state.cooldown_until is None


def test_to_provider_patch_preserves_unhealthy_status() -> None:
    state = CooldownState(
        consecutive_failures=3,
        cooldown_trips=1,
        cooldown_until=_NOW + timedelta(minutes=5),
        health_status="unhealthy",
    )

    patch = to_provider_patch(state)

    assert patch["health_status"] == "unhealthy"
    assert patch["consecutive_failures"] == 3
    assert patch["cooldown_trips"] == 1
    assert patch["cooldown_until"] == _NOW + timedelta(minutes=5)


def test_health_status_transitions_from_unknown_to_healthy_on_success() -> None:
    state = CooldownState()

    assert state.health_status == "unknown"

    recovered = record_provider_success(state, now=_NOW)

    assert recovered.health_status == "healthy"


def test_failure_threshold_of_one_triggers_immediate_cooldown() -> None:
    state = CooldownState(health_status="healthy")

    result = record_provider_failure(state, now=_NOW, failure_threshold=1)

    assert result.consecutive_failures == 1
    assert result.cooldown_trips == 1
    assert result.health_status == "unhealthy"
    assert result.cooldown_until is not None
