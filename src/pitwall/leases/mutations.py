"""Cross-surface lease mutation contract shared by REST and MCP."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pitwall.core.models import Lease
from pitwall.db.repository import (
    LEASE_MUTATION_UNSET,
    LeaseMutationExpiryLimitError,
    LeaseMutationIdempotencyError,
    LeaseMutationStateError,
    LeaseRepository,
)

MAX_LEASE_EXTENSION_MINUTES = 43_200
MAX_LEASE_EXPIRY_HORIZON_MINUTES = 43_200


class LeaseMutationNotFound(RuntimeError):
    """The requested lease does not exist."""


class LeaseMutationConflict(RuntimeError):
    """The current lifecycle state cannot satisfy the mutation."""

    def __init__(self, state: str, operation: str) -> None:
        super().__init__(f"{state}:{operation}")
        self.state = state
        self.operation = operation


class LeaseMutationExpiryLimitExceeded(RuntimeError):
    """The requested expiry would exceed the absolute renewal horizon."""


class LeaseMutationIdempotencyConflict(RuntimeError):
    """The key was already used for a different mutation."""

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(idempotency_key)
        self.idempotency_key = idempotency_key


def _request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def patch_lease_settings(
    repo: LeaseRepository,
    lease_id: str,
    *,
    renewal_policy: str | object,
    auto_teardown_on_expiry: bool | object,
    actor: str,
    idempotency_key: str | None = None,
) -> Lease:
    """Persist supported lease settings atomically with a same-transaction audit."""

    payload = {
        "lease_id": lease_id,
        "renewal_policy": _json_value(renewal_policy),
        "auto_teardown_on_expiry": _json_value(auto_teardown_on_expiry),
    }
    try:
        result = await repo.patch_settings(
            lease_id,
            renewal_policy=renewal_policy,
            auto_teardown_on_expiry=auto_teardown_on_expiry,
            actor=actor,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(payload),
        )
    except LeaseMutationStateError as exc:
        raise LeaseMutationConflict(exc.state, exc.operation) from exc
    except LeaseMutationIdempotencyError as exc:
        raise LeaseMutationIdempotencyConflict(exc.idempotency_key) from exc
    if result is None:
        raise LeaseMutationNotFound(lease_id)
    return result.lease


async def renew_lease(
    repo: LeaseRepository,
    lease_id: str,
    *,
    extends_minutes: int,
    actor: str,
    idempotency_key: str | None = None,
) -> Lease:
    """Add minutes to current expiry, with locking and an absolute 30-day horizon."""

    if not 1 <= extends_minutes <= MAX_LEASE_EXTENSION_MINUTES:
        raise ValueError(f"extends_minutes must be between 1 and {MAX_LEASE_EXTENSION_MINUTES}")
    try:
        result = await repo.renew(
            lease_id,
            extends_minutes=extends_minutes,
            actor=actor,
            idempotency_key=idempotency_key,
            request_hash=_request_hash({"lease_id": lease_id, "extends_minutes": extends_minutes}),
            max_horizon_minutes=MAX_LEASE_EXPIRY_HORIZON_MINUTES,
        )
    except LeaseMutationStateError as exc:
        raise LeaseMutationConflict(exc.state, exc.operation) from exc
    except LeaseMutationExpiryLimitError as exc:
        raise LeaseMutationExpiryLimitExceeded(lease_id) from exc
    except LeaseMutationIdempotencyError as exc:
        raise LeaseMutationIdempotencyConflict(exc.idempotency_key) from exc
    if result is None:
        raise LeaseMutationNotFound(lease_id)
    return result.lease


def _json_value(value: object) -> object:
    if hasattr(value, "value"):
        return value.value
    if value is LEASE_MUTATION_UNSET:
        return None
    return value


__all__ = [
    "MAX_LEASE_EXPIRY_HORIZON_MINUTES",
    "MAX_LEASE_EXTENSION_MINUTES",
    "LEASE_MUTATION_UNSET",
    "LeaseMutationConflict",
    "LeaseMutationExpiryLimitExceeded",
    "LeaseMutationIdempotencyConflict",
    "LeaseMutationNotFound",
    "patch_lease_settings",
    "renew_lease",
]
