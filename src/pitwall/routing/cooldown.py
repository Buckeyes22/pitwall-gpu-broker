"""Provider health cooldown state machine.

The state machine is deliberately pure so probe jobs can calculate the next
provider health state without talking to Postgres. Persistence is handled by
the provider record fields exposed through :func:`to_provider_patch`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_INITIAL_COOLDOWN = dt.timedelta(minutes=5)
DEFAULT_ESCALATED_COOLDOWN = dt.timedelta(minutes=15)
UNHEALTHY_STATUS = "unhealthy"
HEALTHY_STATUS = "healthy"
UNKNOWN_STATUS = "unknown"

_ProviderLike = object | Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderCooldownState:
    """Cooldown fields carried by a provider across health probe runs."""

    consecutive_failures: int = 0
    cooldown_trips: int = 0
    cooldown_until: dt.datetime | None = None
    health_status: str = UNKNOWN_STATUS

    def __post_init__(self) -> None:
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures must be >= 0")
        if self.cooldown_trips < 0:
            raise ValueError("cooldown_trips must be >= 0")
        if self.cooldown_until is not None:
            object.__setattr__(
                self,
                "cooldown_until",
                _normalize_utc(self.cooldown_until, field_name="cooldown_until"),
            )
        if not self.health_status:
            raise ValueError("health_status must be non-empty")


CooldownState = ProviderCooldownState


@dataclass(frozen=True, slots=True)
class CooldownPolicy:
    """Cooldown timing policy for provider health failures."""

    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    initial_cooldown: dt.timedelta = DEFAULT_INITIAL_COOLDOWN
    escalated_cooldown: dt.timedelta = DEFAULT_ESCALATED_COOLDOWN

    def __post_init__(self) -> None:
        _validate_policy(
            failure_threshold=self.failure_threshold,
            initial_cooldown=self.initial_cooldown,
            escalated_cooldown=self.escalated_cooldown,
        )


class CooldownStateMachine:
    """Object-oriented facade for cooldown state transitions."""

    def __init__(self, policy: CooldownPolicy | None = None) -> None:
        self.policy = policy or DEFAULT_COOLDOWN_POLICY

    def record_success(
        self,
        state: ProviderCooldownState | _ProviderLike,
        *,
        now: dt.datetime | None = None,
    ) -> ProviderCooldownState:
        return record_provider_success(state, now=now)

    def record_failure(
        self,
        state: ProviderCooldownState | _ProviderLike,
        *,
        now: dt.datetime | None = None,
    ) -> ProviderCooldownState:
        return record_provider_failure(
            state,
            now=now,
            failure_threshold=self.policy.failure_threshold,
            initial_cooldown=self.policy.initial_cooldown,
            escalated_cooldown=self.policy.escalated_cooldown,
        )

    def apply_probe_result(
        self,
        state: ProviderCooldownState | _ProviderLike,
        *,
        passed: bool,
        now: dt.datetime | None = None,
    ) -> ProviderCooldownState:
        return apply_probe_result(
            state,
            passed=passed,
            now=now,
            failure_threshold=self.policy.failure_threshold,
            initial_cooldown=self.policy.initial_cooldown,
            escalated_cooldown=self.policy.escalated_cooldown,
        )


def state_from_provider(provider: _ProviderLike) -> ProviderCooldownState:
    """Build cooldown state from a provider model or provider-shaped mapping."""

    cooldown_until = _datetime_field(provider, "cooldown_until")
    return ProviderCooldownState(
        consecutive_failures=_int_field(provider, "consecutive_failures", default=0),
        cooldown_trips=_int_field(provider, "cooldown_trips", default=0),
        cooldown_until=cooldown_until,
        health_status=_str_field(provider, "health_status", default=UNKNOWN_STATUS),
    )


def record_provider_success(
    state: ProviderCooldownState | _ProviderLike,
    *,
    now: dt.datetime | None = None,
) -> ProviderCooldownState:
    """Transition a provider after a successful health probe or request."""

    _normalize_now(now)
    return ProviderCooldownState(
        consecutive_failures=0,
        cooldown_trips=0,
        cooldown_until=None,
        health_status=HEALTHY_STATUS,
    )


def record_provider_failure(
    state: ProviderCooldownState | _ProviderLike,
    *,
    now: dt.datetime | None = None,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    initial_cooldown: dt.timedelta = DEFAULT_INITIAL_COOLDOWN,
    escalated_cooldown: dt.timedelta = DEFAULT_ESCALATED_COOLDOWN,
) -> ProviderCooldownState:
    """Transition a provider after a failed health probe or request.

    The first cooldown trip starts after three consecutive failures and lasts
    five minutes. If failures continue after that cooldown without a successful
    probe resetting the state, later trips are capped at fifteen minutes.
    """

    current = _coerce_state(state)
    observed_at = _normalize_now(now)
    _validate_policy(
        failure_threshold=failure_threshold,
        initial_cooldown=initial_cooldown,
        escalated_cooldown=escalated_cooldown,
    )

    if is_in_cooldown(current, now=observed_at):
        return current

    cooldown_until = (
        None
        if current.cooldown_until is not None and current.cooldown_until <= observed_at
        else current.cooldown_until
    )
    consecutive_failures = current.consecutive_failures + 1
    cooldown_trips = current.cooldown_trips
    next_trip = consecutive_failures // failure_threshold

    if next_trip > cooldown_trips:
        cooldown_trips = next_trip
        cooldown_until = observed_at + cooldown_duration_for_trip(
            cooldown_trips,
            initial_cooldown=initial_cooldown,
            escalated_cooldown=escalated_cooldown,
        )
        health_status = UNHEALTHY_STATUS
    else:
        health_status = current.health_status

    return ProviderCooldownState(
        consecutive_failures=consecutive_failures,
        cooldown_trips=cooldown_trips,
        cooldown_until=cooldown_until,
        health_status=health_status,
    )


def apply_probe_result(
    state: ProviderCooldownState | _ProviderLike,
    *,
    passed: bool,
    now: dt.datetime | None = None,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    initial_cooldown: dt.timedelta = DEFAULT_INITIAL_COOLDOWN,
    escalated_cooldown: dt.timedelta = DEFAULT_ESCALATED_COOLDOWN,
) -> ProviderCooldownState:
    """Apply one probe result and return the next cooldown state."""

    if passed:
        return record_provider_success(state, now=now)
    return record_provider_failure(
        state,
        now=now,
        failure_threshold=failure_threshold,
        initial_cooldown=initial_cooldown,
        escalated_cooldown=escalated_cooldown,
    )


def is_in_cooldown(
    state: ProviderCooldownState | _ProviderLike,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """Return true when the state's cooldown window is still active."""

    current = _coerce_state(state)
    observed_at = _normalize_now(now)
    return current.cooldown_until is not None and current.cooldown_until > observed_at


def cooldown_duration_for_trip(
    cooldown_trip: int,
    *,
    initial_cooldown: dt.timedelta = DEFAULT_INITIAL_COOLDOWN,
    escalated_cooldown: dt.timedelta = DEFAULT_ESCALATED_COOLDOWN,
) -> dt.timedelta:
    """Return the cooldown duration for a one-indexed cooldown trip number."""

    if cooldown_trip < 1:
        raise ValueError("cooldown_trip must be >= 1")
    if initial_cooldown <= dt.timedelta(0):
        raise ValueError("initial_cooldown must be positive")
    if escalated_cooldown < initial_cooldown:
        raise ValueError("escalated_cooldown must be >= initial_cooldown")
    if cooldown_trip == 1:
        return initial_cooldown
    return escalated_cooldown


def to_provider_patch(state: ProviderCooldownState | _ProviderLike) -> dict[str, object]:
    """Return repository patch kwargs for the current cooldown state."""

    current = _coerce_state(state)
    return {
        "consecutive_failures": current.consecutive_failures,
        "cooldown_trips": current.cooldown_trips,
        "cooldown_until": current.cooldown_until,
        "health_status": current.health_status,
    }


def _coerce_state(
    state: ProviderCooldownState | _ProviderLike,
) -> ProviderCooldownState:
    if isinstance(state, ProviderCooldownState):
        return state
    return state_from_provider(state)


def _validate_policy(
    *,
    failure_threshold: int,
    initial_cooldown: dt.timedelta,
    escalated_cooldown: dt.timedelta,
) -> None:
    if failure_threshold < 1:
        raise ValueError("failure_threshold must be >= 1")
    cooldown_duration_for_trip(
        1,
        initial_cooldown=initial_cooldown,
        escalated_cooldown=escalated_cooldown,
    )


DEFAULT_COOLDOWN_POLICY = CooldownPolicy()


def _normalize_now(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.UTC)
    return _normalize_utc(value, field_name="now")


def _normalize_utc(value: dt.datetime, *, field_name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(dt.UTC)


def _field(provider: _ProviderLike, key: str) -> object:
    if isinstance(provider, Mapping):
        return provider.get(key)
    return getattr(provider, key, None)


def _int_field(provider: _ProviderLike, key: str, *, default: int) -> int:
    value = _field(provider, key)
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return int(cast(int | float | str | bool, value))


def _str_field(provider: _ProviderLike, key: str, *, default: str) -> str:
    value = _field(provider, key)
    if isinstance(value, Enum):
        value = value.value
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _datetime_field(provider: _ProviderLike, key: str) -> dt.datetime | None:
    value = _field(provider, key)
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return _normalize_utc(value, field_name=key)
    if isinstance(value, str):
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _normalize_utc(parsed, field_name=key)
    raise ValueError(f"{key} must be a datetime or ISO-8601 string")


record_success = record_provider_success
record_failure = record_provider_failure
next_cooldown_state = apply_probe_result
is_provider_in_cooldown = is_in_cooldown


__all__ = [
    "CooldownState",
    "CooldownPolicy",
    "CooldownStateMachine",
    "DEFAULT_COOLDOWN_POLICY",
    "DEFAULT_ESCALATED_COOLDOWN",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_INITIAL_COOLDOWN",
    "HEALTHY_STATUS",
    "ProviderCooldownState",
    "UNHEALTHY_STATUS",
    "UNKNOWN_STATUS",
    "apply_probe_result",
    "cooldown_duration_for_trip",
    "is_in_cooldown",
    "is_provider_in_cooldown",
    "next_cooldown_state",
    "record_failure",
    "record_provider_failure",
    "record_provider_success",
    "record_success",
    "state_from_provider",
    "to_provider_patch",
]
