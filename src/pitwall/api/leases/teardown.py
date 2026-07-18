"""Single-lease pod teardown service.

This path is deliberately scoped to one persisted lease. Account-wide emergency
termination belongs to the admin kill switch, not this module.
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pitwall.api.exceptions import LeaseNotFound, LeaseStateConflict
from pitwall.core.enums import LeaseState
from pitwall.core.models import Lease, Provider
from pitwall.db.repository import LeaseRepository, ProviderRepository
from pitwall.leases.state import TERMINAL_LEASE_STATES, transition_lease_state
from pitwall.runpod_client.pods import _terminate_pod, terminate_pod

log = logging.getLogger("pitwall.api.leases.teardown")

LEASE_TERMINATED_CHANNEL = "pitwall:lease:terminated"
LEASE_TERMINATED_EVENT_TYPE = "lease.terminated"
_DEFAULT_TERMINATION_REASON = "operator_stop"
_DEFAULT_EXPIRATION_REASON = "lease_expired"
_TEARDOWN_TERMINAL_STATES = frozenset({LeaseState.STOPPED, LeaseState.EXPIRED})
_USD_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class LeaseTeardownResult:
    """Result returned after a scoped lease teardown attempt."""

    lease: Lease
    event: dict[str, str | None] | None
    published_subscribers: int = 0


class TeardownFailed(RuntimeError):
    """Raised when the single-lease teardown cannot be completed."""


async def run_teardown(
    lease_id: str,
    *,
    pool: Any,
    redis_client: Any | None = None,
    reason: str | None = None,
    now: dt.datetime | None = None,
    terminal_state: LeaseState | str = LeaseState.STOPPED,
    api_key: str | None = None,
    rest_api_url: str | None = None,
) -> LeaseTeardownResult:
    """Terminate the pod for one lease, close its cost, and publish its event."""

    target_state = _teardown_terminal_state(terminal_state)
    lease_repo = LeaseRepository(pool)
    provider_repo = ProviderRepository(pool)
    existing = await lease_repo.get(lease_id)
    if existing is None:
        raise LeaseNotFound(lease_id)

    if _lease_state(existing) in TERMINAL_LEASE_STATES:
        return LeaseTeardownResult(lease=existing, event=None, published_subscribers=0)

    stopping = await _mark_stopping(lease_repo, existing)
    terminate_kwargs: dict[str, str] = {}
    if api_key is not None:
        terminate_kwargs["api_key"] = api_key
    if rest_api_url is not None:
        terminate_kwargs["rest_api_url"] = rest_api_url
    if terminate_kwargs:
        await _terminate_pod(stopping.runpod_pod_id, **terminate_kwargs)
    else:
        await terminate_pod(stopping.runpod_pod_id)

    terminated_at = now or dt.datetime.now(dt.UTC)
    termination_reason = _normalize_reason(reason, terminal_state=target_state)
    provider = await provider_repo.get(stopping.provider_id)
    cost_accrued_usd = close_lease_cost(
        stopping,
        provider=provider,
        terminated_at=terminated_at,
    )
    closed_state = transition_lease_state(stopping.state, target_state)
    closed = await lease_repo.close_teardown(
        stopping.id,
        state=closed_state.value,
        cost_accrued_usd=cost_accrued_usd,
        terminated_at=terminated_at,
        terminated_reason=termination_reason,
    )
    if closed is None:
        raise LeaseNotFound(stopping.id)

    event = lease_terminated_event(closed)
    subscribers = await publish_lease_terminated(redis_client, event)
    log.info(
        "lease teardown complete: lease=%s pod=%s cost_usd=%s subscribers=%s",
        closed.id,
        closed.runpod_pod_id,
        cost_accrued_usd,
        subscribers,
    )
    return LeaseTeardownResult(
        lease=closed,
        event=event,
        published_subscribers=subscribers,
    )


def close_lease_cost(
    lease: Lease,
    *,
    provider: Provider | None,
    terminated_at: dt.datetime,
) -> Decimal:
    """Return the final accrued cost for a lease at teardown time."""

    rate = _provider_cost_rate_per_second(provider)
    if rate is None:
        return _usd(lease.cost_accrued_usd or Decimal("0"))

    elapsed = terminated_at - lease.created_at
    elapsed_seconds = Decimal(str(max(elapsed.total_seconds(), 0.0)))
    return _usd(rate * elapsed_seconds)


def lease_terminated_event(lease: Lease) -> dict[str, str | None]:
    """Build the Redis pub/sub payload for a terminated lease."""

    state = _state_value(lease.state)
    return {
        "event": LEASE_TERMINATED_EVENT_TYPE,
        "lease_id": lease.id,
        "provider_id": lease.provider_id,
        "runpod_pod_id": lease.runpod_pod_id,
        "state": state,
        "terminated_at": (
            lease.terminated_at.isoformat() if lease.terminated_at is not None else None
        ),
        "terminated_reason": lease.terminated_reason,
        "cost_accrued_usd": (
            str(lease.cost_accrued_usd) if lease.cost_accrued_usd is not None else None
        ),
    }


async def publish_lease_terminated(
    redis_client: Any | None,
    event: Mapping[str, str | None],
) -> int:
    """Publish a lease termination event if a Redis client is available."""

    if redis_client is None:
        log.warning(
            "lease termination event not published because redis_client is unavailable: lease=%s",
            event.get("lease_id"),
        )
        return 0

    payload = json.dumps(dict(event), sort_keys=True, separators=(",", ":"))
    published = redis_client.publish(LEASE_TERMINATED_CHANNEL, payload)
    if inspect.isawaitable(published):
        published = await published
    return int(published) if isinstance(published, int) else 0


async def _mark_stopping(repo: LeaseRepository, lease: Lease) -> Lease:
    state = _lease_state(lease)
    if state == LeaseState.STOPPING:
        return lease
    if state != LeaseState.ACTIVE:
        raise LeaseStateConflict(lease.id, state.value, "stop")

    stopping = transition_lease_state(state, LeaseState.STOPPING)
    updated = await repo.update_state(lease.id, stopping.value)
    if updated is None:
        raise LeaseNotFound(lease.id)
    return updated


def _provider_cost_rate_per_second(provider: Provider | None) -> Decimal | None:
    if provider is None:
        return None

    config = provider.config if isinstance(provider.config, Mapping) else {}
    cost = config.get("cost")
    sources = [cost, config] if isinstance(cost, Mapping) else [config]
    for source in sources:
        raw_rate = source.get("per_second_active")
        if raw_rate is not None:
            return _non_negative_decimal(raw_rate, "provider cost 'per_second_active'")
    return None


def _non_negative_decimal(raw_value: object, name: str) -> Decimal:
    if isinstance(raw_value, bool):
        raise ValueError(f"{name} must be a decimal value")
    try:
        value = Decimal(str(raw_value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal value") from exc
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _usd(value: Decimal) -> Decimal:
    return value.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)


def _normalize_reason(reason: str | None, *, terminal_state: LeaseState) -> str:
    if reason is None:
        if terminal_state == LeaseState.EXPIRED:
            return _DEFAULT_EXPIRATION_REASON
        return _DEFAULT_TERMINATION_REASON
    normalized = reason.strip()
    if normalized:
        return normalized
    if terminal_state == LeaseState.EXPIRED:
        return _DEFAULT_EXPIRATION_REASON
    return _DEFAULT_TERMINATION_REASON


def _lease_state(lease: Lease) -> LeaseState:
    if isinstance(lease.state, LeaseState):
        return lease.state
    return LeaseState(str(lease.state))


def _state_value(state: LeaseState | str) -> str:
    return state.value if isinstance(state, LeaseState) else state


def _teardown_terminal_state(state: LeaseState | str) -> LeaseState:
    coerced = state if isinstance(state, LeaseState) else LeaseState(str(state))
    if coerced not in _TEARDOWN_TERMINAL_STATES:
        allowed = ", ".join(sorted(state.value for state in _TEARDOWN_TERMINAL_STATES))
        raise ValueError(f"teardown terminal_state must be one of: {allowed}")
    return coerced


teardown_lease = run_teardown


__all__ = [
    "LEASE_TERMINATED_CHANNEL",
    "LEASE_TERMINATED_EVENT_TYPE",
    "LeaseTeardownResult",
    "TeardownFailed",
    "close_lease_cost",
    "lease_terminated_event",
    "publish_lease_terminated",
    "run_teardown",
    "teardown_lease",
]
