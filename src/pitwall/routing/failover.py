"""Spot/preemptible capacity failover with checkpoint resume.

The controller is deliberately orchestration-only: it asks the provider plugin
for current status, lets the caller capture application-specific checkpoint
state, provisions a selected on-demand target through the provider registry,
then passes the checkpoint and target lease to a caller-supplied resume hook.
"""

from __future__ import annotations

import datetime as dt
import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

from pitwall.core.models import Capability
from pitwall.core.models import Provider as ProviderRecord
from pitwall.routing.arbitrage import (
    ArbitrageOption,
    ArbitrageScore,
    score_arbitrage_option,
)

if TYPE_CHECKING:
    from pitwall.providers.interface import (
        Provider,
        ProviderOperationContext,
        ProvisionResult,
        StatusResult,
    )
    from pitwall.providers.registry import ProviderRegistry

_PREEMPTION_MARKERS = frozenset(
    {
        "evicted",
        "interrupted",
        "outbid",
        "preempted",
        "preempted_by_bid",
    }
)
_PREEMPTION_TEXT_FIELDS = (
    "actual_status",
    "cur_state",
    "failure_reason",
    "intended_status",
    "lifecycle_state",
    "next_state",
    "reason",
    "state",
    "status",
    "status_message",
    "status_msg",
)


class FailoverCapacityMarket(StrEnum):
    """Capacity market attached to a source lease or candidate target."""

    SPOT = "spot"
    PREEMPTIBLE = "preemptible"
    ON_DEMAND = "on_demand"


@dataclass(frozen=True, slots=True)
class FailoverSource:
    """Source capacity whose provider status should be inspected."""

    provider_plugin_id: str
    provider_record: ProviderRecord
    credentials: object
    external_id: str
    lease_id: str | None = None
    market: FailoverCapacityMarket = FailoverCapacityMarket.PREEMPTIBLE

    def __post_init__(self) -> None:
        _validate_non_empty(self.provider_plugin_id, field_name="provider_plugin_id")
        _validate_non_empty(self.external_id, field_name="external_id")
        object.__setattr__(self, "market", _coerce_market(self.market, field_name="market"))


@dataclass(frozen=True, slots=True)
class FailoverCheckpoint:
    """Checkpoint token and state captured from a preempted source lease."""

    token: str
    state: Mapping[str, Any] = field(default_factory=dict)
    captured_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        _validate_non_empty(self.token, field_name="token")
        object.__setattr__(self, "state", MappingProxyType(dict(self.state)))
        if self.captured_at is not None:
            object.__setattr__(
                self,
                "captured_at",
                _normalize_utc(self.captured_at, field_name="captured_at"),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "token": self.token,
            "state": dict(self.state),
            "captured_at": _isoformat_utc(self.captured_at) if self.captured_at else None,
        }


@dataclass(frozen=True, slots=True)
class FailoverCheckpointRequest:
    """Inputs passed to the application-specific checkpoint hook."""

    context: ProviderOperationContext
    source: FailoverSource
    status: StatusResult


@dataclass(frozen=True, slots=True)
class FailoverTarget:
    """One on-demand failover candidate and its arbitrage inputs."""

    provider_plugin_id: str
    provider_record: ProviderRecord
    credentials: object
    gpu: str
    price: Decimal
    latency_ms: Decimal
    market: FailoverCapacityMarket = FailoverCapacityMarket.ON_DEMAND
    provision_payload: Mapping[str, Any] = field(default_factory=dict)
    extra_env: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        _validate_non_empty(self.provider_plugin_id, field_name="provider_plugin_id")
        _validate_non_empty(self.gpu, field_name="gpu")
        object.__setattr__(self, "market", _coerce_market(self.market, field_name="market"))
        object.__setattr__(
            self,
            "provision_payload",
            MappingProxyType(dict(self.provision_payload)),
        )
        if self.extra_env is not None:
            object.__setattr__(self, "extra_env", MappingProxyType(dict(self.extra_env)))

    @property
    def provider_id(self) -> str:
        return self.provider_record.id

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_plugin_id": self.provider_plugin_id,
            "provider_id": self.provider_record.id,
            "market": self.market.value,
            "gpu": self.gpu,
            "price": str(self.price),
            "latency_ms": str(self.latency_ms),
            "provision_payload": dict(self.provision_payload),
            "extra_env": dict(self.extra_env) if self.extra_env is not None else None,
        }


@dataclass(frozen=True, slots=True)
class FailoverTargetSelection:
    """Arbitrage score and rank for a selected failover target."""

    target: FailoverTarget
    score: ArbitrageScore
    rank: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "target": self.target.to_dict(),
            "score": self.score.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class FailoverResumeRequest:
    """Inputs passed to the application-specific resume hook."""

    context: ProviderOperationContext
    source: FailoverSource
    status: StatusResult
    checkpoint: FailoverCheckpoint
    target: FailoverTarget
    selection: FailoverTargetSelection
    provision_result: ProvisionResult


type CheckpointOutcome = FailoverCheckpoint | Awaitable[FailoverCheckpoint]
type CheckpointCallable = Callable[[FailoverCheckpointRequest], CheckpointOutcome]
type ResumeOutcome[ResultT] = ResultT | Awaitable[ResultT]
type ResumeCallable[ResultT] = Callable[[FailoverResumeRequest], ResumeOutcome[ResultT]]


@dataclass(frozen=True, slots=True)
class FailoverRequest[ResultT]:
    """Inputs for one spot-to-on-demand failover attempt."""

    context: ProviderOperationContext
    registry: ProviderRegistry
    capability: Capability
    source: FailoverSource
    targets: Sequence[FailoverTarget]
    checkpoint: CheckpointCallable
    resume: ResumeCallable[ResultT]
    lambda_weight: Decimal = Decimal("0")
    request_id: str | None = None
    budget_gate: Any | None = None
    idempotency_key: str | None = None
    dry_run: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", tuple(self.targets))


@dataclass(frozen=True, slots=True)
class FailoverResult[ResultT]:
    """Outcome of a failover inspection and optional resume."""

    preempted: bool
    resumed: bool
    status: StatusResult
    checkpoint: FailoverCheckpoint | None = None
    target: FailoverTarget | None = None
    selection: FailoverTargetSelection | None = None
    provision_result: ProvisionResult | None = None
    resume_result: ResultT | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "preempted": self.preempted,
            "resumed": self.resumed,
            "status": {
                "provider_id": self.status.provider_id,
                "external_id": self.status.external_id,
                "status": _status_string(self.status.status),
                "raw": dict(self.status.raw),
            },
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
            "target": self.target.to_dict() if self.target else None,
            "selection": self.selection.to_dict() if self.selection else None,
            "provision_result": _provision_result_dict(self.provision_result),
        }


async def execute_spot_failover[ResultT](
    request: FailoverRequest[ResultT],
) -> FailoverResult[ResultT]:
    """Inspect source status and resume on on-demand capacity if preempted."""

    from pitwall.providers.interface import ProvisionRequest, StatusRequest

    source_provider = request.registry.lookup(request.source.provider_plugin_id)
    source_credentials = request.registry.validate_credentials(
        request.source.provider_plugin_id,
        request.source.credentials,
    )
    status = await source_provider.status(
        StatusRequest(
            context=request.context,
            provider_record=request.source.provider_record,
            credentials=source_credentials,
            external_id=request.source.external_id,
        )
    )
    if not is_preempted_status(status):
        return FailoverResult(preempted=False, resumed=False, status=status)

    checkpoint_request = FailoverCheckpointRequest(
        context=request.context,
        source=request.source,
        status=status,
    )
    checkpoint = await _await_checkpoint(request.checkpoint(checkpoint_request))
    selection = select_on_demand_failover_target(
        request.targets,
        lambda_weight=request.lambda_weight,
    )
    target = selection.target
    target_provider = request.registry.lookup(target.provider_plugin_id)
    target_credentials = request.registry.validate_credentials(
        target.provider_plugin_id,
        target.credentials,
    )
    provision_result = await target_provider.provision(
        ProvisionRequest(
            context=request.context,
            capability=request.capability,
            provider_record=target.provider_record,
            credentials=target_credentials,
            request_id=request.request_id,
            extra_env=target.extra_env,
            payload=target.provision_payload,
            budget_gate=request.budget_gate,
            idempotency_key=request.idempotency_key,
            dry_run=request.dry_run,
        )
    )
    resume_request = FailoverResumeRequest(
        context=request.context,
        source=request.source,
        status=status,
        checkpoint=checkpoint,
        target=target,
        selection=selection,
        provision_result=provision_result,
    )
    try:
        resume_result = await _await_resume(request.resume(resume_request))
    except BaseException as exc:
        await _cleanup_failed_resume(
            target_provider,
            context=request.context,
            target=target,
            target_credentials=target_credentials,
            provision_result=provision_result,
            cause=exc,
        )
        raise
    return FailoverResult(
        preempted=True,
        resumed=True,
        status=status,
        checkpoint=checkpoint,
        target=target,
        selection=selection,
        provision_result=provision_result,
        resume_result=resume_result,
    )


def is_preempted_status(status: StatusResult) -> bool:
    """Return whether a provider-neutral status result represents preemption."""

    if _truthy(status.raw.get("pitwall_preempted")):
        return True
    if _status_string(status.status) != "failed":
        return False
    for value in _preemption_text_values(status.raw):
        if any(marker in value for marker in _PREEMPTION_MARKERS):
            return True
    return False


def select_on_demand_failover_target(
    targets: Iterable[FailoverTarget],
    *,
    lambda_weight: Decimal,
) -> FailoverTargetSelection:
    """Select the best on-demand target using price-latency arbitrage."""

    ranked = sort_on_demand_failover_targets(targets, lambda_weight=lambda_weight)
    if not ranked:
        raise ValueError("targets must include at least one on-demand target")
    return ranked[0]


def sort_on_demand_failover_targets(
    targets: Iterable[FailoverTarget],
    *,
    lambda_weight: Decimal,
) -> tuple[FailoverTargetSelection, ...]:
    """Return on-demand targets ranked by arbitrage objective."""

    selections = [
        _selection_for_target(target, lambda_weight=lambda_weight)
        for target in targets
        if target.market is FailoverCapacityMarket.ON_DEMAND
    ]
    ranked = sorted(selections, key=_selection_sort_key)
    return tuple(replace(selection, rank=index) for index, selection in enumerate(ranked, start=1))


def _selection_for_target(
    target: FailoverTarget,
    *,
    lambda_weight: Decimal,
) -> FailoverTargetSelection:
    return FailoverTargetSelection(
        target=target,
        score=score_arbitrage_option(
            ArbitrageOption(
                provider_id=target.provider_record.id,
                gpu=target.gpu,
                price=target.price,
                latency_ms=target.latency_ms,
            ),
            lambda_weight=lambda_weight,
        ),
    )


def _selection_sort_key(
    selection: FailoverTargetSelection,
) -> tuple[Decimal, Decimal, Decimal, str, str]:
    score = selection.score
    return (
        score.objective,
        score.cost_component,
        score.option.latency_ms,
        score.option.provider_id,
        score.option.gpu,
    )


async def _await_checkpoint(outcome: CheckpointOutcome) -> FailoverCheckpoint:
    if inspect.isawaitable(outcome):
        return await outcome
    return outcome


async def _await_resume[ResultT](outcome: ResumeOutcome[ResultT]) -> ResultT:
    if inspect.isawaitable(outcome):
        return await cast(Awaitable[ResultT], outcome)
    return outcome


async def _cleanup_failed_resume(
    target_provider: Provider,
    *,
    context: ProviderOperationContext,
    target: FailoverTarget,
    target_credentials: BaseModel,
    provision_result: ProvisionResult,
    cause: BaseException,
) -> None:
    from pitwall.providers.interface import ReconcileRequest, TeardownRequest

    try:
        if provision_result.lease_id is not None:
            await target_provider.teardown(
                TeardownRequest(
                    context=context,
                    provider_record=target.provider_record,
                    credentials=target_credentials,
                    lease_id=provision_result.lease_id,
                    reason="failover_resume_failed",
                )
            )
            return
        if provision_result.external_id is not None:
            await target_provider.reconcile(
                ReconcileRequest(
                    context=context,
                    provider_record=target.provider_record,
                    credentials=target_credentials,
                    external_ids=(provision_result.external_id,),
                )
            )
    except BaseException as cleanup_error:
        raise cleanup_error from cause


def _preemption_text_values(raw: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for field_name in _PREEMPTION_TEXT_FIELDS:
        normalized = _normalized_text(raw.get(field_name))
        if normalized:
            values.append(normalized)
    return tuple(values)


def _truthy(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _coerce_market(value: object, *, field_name: str) -> FailoverCapacityMarket:
    if isinstance(value, FailoverCapacityMarket):
        return value
    if isinstance(value, str):
        try:
            return FailoverCapacityMarket(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} is not a valid failover capacity market") from exc
    raise TypeError(f"{field_name} must be a FailoverCapacityMarket or string")


def _validate_non_empty(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not include surrounding whitespace")


def _normalize_utc(value: dt.datetime, *, field_name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(dt.UTC)


def _isoformat_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat()


def _normalized_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    return ""


def _status_string(value: object) -> str:
    enum_value = getattr(value, "value", value)
    if isinstance(enum_value, str):
        return enum_value.strip().lower()
    return str(enum_value).strip().lower()


def _provision_result_dict(result: ProvisionResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "provider_id": result.provider_id,
        "external_id": result.external_id,
        "lease_id": result.lease_id,
        "raw": dict(result.raw),
    }


__all__ = [
    "CheckpointCallable",
    "CheckpointOutcome",
    "FailoverCapacityMarket",
    "FailoverCheckpoint",
    "FailoverCheckpointRequest",
    "FailoverRequest",
    "FailoverResult",
    "FailoverResumeRequest",
    "FailoverSource",
    "FailoverTarget",
    "FailoverTargetSelection",
    "ResumeCallable",
    "ResumeOutcome",
    "execute_spot_failover",
    "is_preempted_status",
    "select_on_demand_failover_target",
    "sort_on_demand_failover_targets",
]
