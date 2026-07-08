"""Provider plugin protocol and operation payloads."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from pitwall.core.enums import LeaseState
from pitwall.core.models import Capability
from pitwall.core.models import Provider as ProviderRecord
from pitwall.cost.estimator import TaggedPricingModel

JsonObject = dict[str, Any]


class ResourceStatus(StrEnum):
    """Provider-independent resource lifecycle statuses."""

    UNKNOWN = "unknown"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    TERMINATED = "terminated"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProviderOperationContext:
    """Runtime services available to provider operations."""

    pool: Any
    redis_client: Any | None = None
    now: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class ProvisionRequest:
    """Inputs required to provision a provider-backed lease/resource."""

    context: ProviderOperationContext
    capability: Capability
    provider_record: ProviderRecord
    credentials: BaseModel
    request_id: str | None = None
    extra_env: Mapping[str, str] | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    budget_gate: Any | None = None
    idempotency_key: str | None = None
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    """Provider-independent provision result."""

    provider_id: str
    external_id: str | None
    lease_id: str | None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StatusRequest:
    """Inputs required to read one provider-backed resource status."""

    context: ProviderOperationContext
    provider_record: ProviderRecord
    credentials: BaseModel
    external_id: str


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Provider-independent status result."""

    provider_id: str
    external_id: str
    status: ResourceStatus
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReconcileRequest:
    """Inputs required to reconcile provider resources."""

    context: ProviderOperationContext
    provider_record: ProviderRecord
    credentials: BaseModel
    external_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Provider-independent reconcile summary."""

    provider_id: str
    checked: int
    updated: int
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TeardownRequest:
    """Inputs required to tear down a provider-backed lease/resource."""

    context: ProviderOperationContext
    provider_record: ProviderRecord
    credentials: BaseModel
    lease_id: str
    reason: str | None = None
    terminal_state: LeaseState | str = LeaseState.STOPPED


@dataclass(frozen=True, slots=True)
class TeardownResult:
    """Provider-independent teardown result."""

    provider_id: str
    lease_id: str
    external_id: str | None
    raw: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    """Operations every provider plugin must expose to the broker."""

    @property
    def id(self) -> str:
        """Stable provider plugin id, e.g. ``runpod``."""
        ...

    @property
    def name(self) -> str:
        """Human-readable provider plugin name."""
        ...

    @property
    def credential_schema(self) -> type[BaseModel]:
        """Pydantic schema used to validate provider credentials."""
        ...

    def pricing_model(
        self,
        capability: Capability,
        provider_record: ProviderRecord,
    ) -> TaggedPricingModel:
        """Return the provider's tagged pricing model for one record."""
        ...

    async def provision(self, request: ProvisionRequest) -> ProvisionResult:
        """Provision or lease provider capacity."""
        ...

    async def status(self, request: StatusRequest) -> StatusResult:
        """Return the current status for one provider resource."""
        ...

    async def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        """Reconcile provider resources with broker state."""
        ...

    async def teardown(self, request: TeardownRequest) -> TeardownResult:
        """Tear down provider capacity."""
        ...


__all__ = [
    "JsonObject",
    "Provider",
    "ProviderOperationContext",
    "ProvisionRequest",
    "ProvisionResult",
    "ReconcileRequest",
    "ReconcileResult",
    "ResourceStatus",
    "StatusRequest",
    "StatusResult",
    "TeardownRequest",
    "TeardownResult",
]
