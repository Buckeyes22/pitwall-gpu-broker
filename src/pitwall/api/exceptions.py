"""Mapped API exceptions for Pitwall REST surface.

Each exception class carries its own HTTP status code and response body shape
so FastAPI exception handlers can produce consistent error responses.

The public OpenAPI document describes the resulting response contracts.
"""

from __future__ import annotations

from typing import Any


class PitwallApiError(RuntimeError):
    """Base for all mapped API exceptions."""

    status_code: int = 500
    error_code: str = "internal_error"

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code}


class CapabilityNotFound(PitwallApiError):
    """Capability name does not exist in the registry."""

    status_code = 404
    error_code = "capability_not_found"

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "name": self.name}


class InvalidProxyPath(PitwallApiError):
    """OpenAI proxy path failed safety validation (absolute URL, traversal, …)."""

    status_code = 400
    error_code = "invalid_proxy_path"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "detail": self.detail}


class CapabilityDisabled(PitwallApiError):
    """Capability exists but is disabled."""

    status_code = 409
    error_code = "capability_disabled"

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "name": self.name}


class CapabilityConflict(PitwallApiError):
    """Duplicate capability name on create."""

    status_code = 409
    error_code = "capability_conflict"

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "name": self.name}


class ProviderNotFound(PitwallApiError):
    """Provider ID does not exist in the registry."""

    status_code = 404
    error_code = "provider_not_found"

    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id)
        self.provider_id = provider_id

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "id": self.provider_id}


class ProviderUnavailable(PitwallApiError):
    """No enabled, healthy provider can currently serve a capability."""

    status_code = 503
    error_code = "no_providers_available"

    def __init__(self, capability: str, chain: list[str] | None = None) -> None:
        super().__init__(capability)
        self.capability = capability
        self.chain = chain or []

    def to_response_body(self) -> dict[str, Any]:
        return {
            "error": self.error_code,
            "capability": self.capability,
            "chain": self.chain,
        }


class ProviderConflict(PitwallApiError):
    """Duplicate provider name on create."""

    status_code = 409
    error_code = "provider_conflict"

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "name": self.name}


class ProviderCapabilityMissing(PitwallApiError):
    """Provider creation references a capability id that is not registered."""

    status_code = 422
    error_code = "provider_capability_missing"

    def __init__(self, capability_id: str) -> None:
        super().__init__(capability_id)
        self.capability_id = capability_id

    def to_response_body(self) -> dict[str, Any]:
        return {
            "error": self.error_code,
            "capability_id": self.capability_id,
            "message": (
                f"capability '{self.capability_id}' does not exist; create it first "
                "with POST /v1/admin/capabilities"
            ),
        }


class RateLimited(PitwallApiError):
    """Local token bucket could not admit a request within the wait budget."""

    status_code = 503
    error_code = "rate_limited"

    def __init__(self, *, retry_after_s: float) -> None:
        super().__init__(f"rate_limited retry_after_s={retry_after_s}")
        self.retry_after_s = retry_after_s

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "retry_after_s": self.retry_after_s}


class LeaseNotFound(PitwallApiError):
    """Lease ID does not exist."""

    status_code = 404
    error_code = "lease_not_found"

    def __init__(self, lease_id: str) -> None:
        super().__init__(lease_id)
        self.lease_id = lease_id

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "id": self.lease_id}


class LeaseStateConflict(PitwallApiError):
    """Lease lifecycle state cannot satisfy the requested operation."""

    status_code = 409
    error_code = "lease_state_conflict"

    def __init__(self, lease_id: str, state: str, operation: str) -> None:
        super().__init__(f"{lease_id}:{state}:{operation}")
        self.lease_id = lease_id
        self.state = state
        self.operation = operation

    def to_response_body(self) -> dict[str, Any]:
        return {
            "error": self.error_code,
            "id": self.lease_id,
            "state": self.state,
            "operation": self.operation,
        }


class ChangeSetTooBroad(PitwallApiError):
    """Lease PATCH attempts to change more than one paid-launch axis."""

    status_code = 400
    error_code = "change_set_too_broad"

    def __init__(self, conflicting_fields: list[str]) -> None:
        super().__init__(",".join(conflicting_fields))
        self.conflicting_fields = conflicting_fields

    def to_response_body(self) -> dict[str, Any]:
        return {
            "error": self.error_code,
            "conflicting_fields": self.conflicting_fields,
        }


class UnsupportedLeasePatch(PitwallApiError):
    """Lease PATCH contains fields that are not mutable in the public contract."""

    status_code = 422
    error_code = "unsupported_lease_patch"

    def __init__(self, fields: list[str]) -> None:
        super().__init__(",".join(fields))
        self.fields = fields

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "fields": self.fields}


class EmptyLeasePatch(PitwallApiError):
    """Lease PATCH contains no setting to mutate."""

    status_code = 422
    error_code = "empty_lease_patch"


class LeaseExpiryLimitExceeded(PitwallApiError):
    """Lease renewal would put expiry beyond the allowed future horizon."""

    status_code = 409
    error_code = "lease_expiry_limit_exceeded"

    def __init__(self, lease_id: str, max_horizon_minutes: int) -> None:
        super().__init__(lease_id)
        self.lease_id = lease_id
        self.max_horizon_minutes = max_horizon_minutes

    def to_response_body(self) -> dict[str, Any]:
        return {
            "error": self.error_code,
            "id": self.lease_id,
            "max_horizon_minutes": self.max_horizon_minutes,
        }


class IdempotencyConflict(PitwallApiError):
    """An idempotency key was reused for a different mutation."""

    status_code = 422
    error_code = "idempotency_conflict"

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(idempotency_key)
        self.idempotency_key = idempotency_key

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "idempotency_key": self.idempotency_key}


class WebhookSubscriptionNotFound(PitwallApiError):
    """Webhook subscription ID does not exist."""

    status_code = 404
    error_code = "webhook_subscription_not_found"

    def __init__(self, subscription_id: int) -> None:
        super().__init__(str(subscription_id))
        self.subscription_id = subscription_id

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "id": self.subscription_id}


class IdempotencyMismatch(PitwallApiError):
    """Idempotency key was reused with a different request body."""

    status_code = 422
    error_code = "idempotency_mismatch"

    def __init__(self, original_workload_id: str) -> None:
        super().__init__(original_workload_id)
        self.original_workload_id = original_workload_id

    def to_response_body(self) -> dict[str, Any]:
        return {
            "error": self.error_code,
            "original_workload_id": self.original_workload_id,
        }


class PreSpendPayloadRejected(PitwallApiError):
    """Inbound payload contains blocked pre-spend PII/secret findings."""

    status_code = 422
    error_code = "pre_spend_payload_rejected"

    def __init__(self, *, decision: str, findings: list[dict[str, Any]]) -> None:
        super().__init__(decision)
        self.decision = decision
        self.findings = findings

    def to_response_body(self) -> dict[str, Any]:
        return {
            "error": self.error_code,
            "decision": self.decision,
            "findings": self.findings,
        }


class WorkloadNotFound(PitwallApiError):
    """Workload ID does not exist."""

    status_code = 404
    error_code = "workload_not_found"

    def __init__(self, workload_id: str) -> None:
        super().__init__(workload_id)
        self.workload_id = workload_id

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "id": self.workload_id}


class JobNotReady(PitwallApiError):
    """Job result requested before the workload reached a terminal state."""

    status_code = 409
    error_code = "job_not_ready"

    def __init__(self, workload_id: str, state: str) -> None:
        super().__init__(f"{workload_id}:{state}")
        self.workload_id = workload_id
        self.state = state

    def to_response_body(self) -> dict[str, Any]:
        return {"error": self.error_code, "id": self.workload_id, "state": self.state}


__all__ = [
    "ChangeSetTooBroad",
    "CapabilityConflict",
    "CapabilityDisabled",
    "CapabilityNotFound",
    "IdempotencyMismatch",
    "IdempotencyConflict",
    "JobNotReady",
    "LeaseNotFound",
    "LeaseExpiryLimitExceeded",
    "LeaseStateConflict",
    "PitwallApiError",
    "PreSpendPayloadRejected",
    "EmptyLeasePatch",
    "ProviderConflict",
    "ProviderCapabilityMissing",
    "ProviderNotFound",
    "ProviderUnavailable",
    "RateLimited",
    "UnsupportedLeasePatch",
    "WebhookSubscriptionNotFound",
    "WorkloadNotFound",
]
