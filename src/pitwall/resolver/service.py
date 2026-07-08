"""Runtime capability resolver for Stage 1+2 provider selection."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, cast

from pitwall.core.models import Capability, Provider
from pitwall.resolver.exceptions import (
    CapabilityDisabledError,
    CapabilityNotFoundError,
    NoHealthyProviderError,
    ProviderNotFoundError,
)
from pitwall.routing import (
    ConstraintResult,
    PlanningContext,
    ProviderEliminated,
    RouteElimination,
    RoutingRequest,
)
from pitwall.routing.constraints import filter_hard_constraints
from pitwall.routing.cooldown import is_in_cooldown


class CapabilityRepositoryLike(Protocol):
    async def get(self, capability_id: str) -> Capability | None: ...

    async def get_by_name(self, name: str) -> Capability | None: ...


class ProviderRepositoryLike(Protocol):
    async def get(self, provider_id: str) -> Provider | None: ...

    async def list(
        self,
        *,
        capability_id: str | None = None,
        enabled_only: bool = False,
        provider_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Provider]: ...


@dataclass(frozen=True, slots=True)
class Stage12Resolution:
    """Selected provider plus the Stage 1+2 decision details."""

    capability: Capability
    provider: Provider
    eligible_providers: tuple[Provider, ...]
    eliminated: tuple[RouteElimination, ...] = field(default_factory=tuple)

    @property
    def selected_provider_id(self) -> str:
        return self.provider.id

    @property
    def provider_id(self) -> str:
        return self.provider.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability.id,
            "capability_name": self.capability.name,
            "selected_provider_id": self.provider.id,
            "eligible_provider_ids": [provider.id for provider in self.eligible_providers],
            "eliminated": [item.to_dict() for item in self.eliminated],
        }


async def resolve_capability(
    capability_name: str,
    *,
    capability_repo: CapabilityRepositoryLike,
    provider_repo: ProviderRepositoryLike,
    provider_id: str | None = None,
    request: RoutingRequest | None = None,
    context: PlanningContext | None = None,
    now: dt.datetime | None = None,
    provider_limit: int = 100,
) -> Stage12Resolution:
    """Resolve a capability request to one enabled, healthy provider.

    The E5 sync-inference path only needs routing Stages 1 and 2: hard
    constraints, then provider health/cooldown. Among survivors, the resolver
    chooses the lowest priority number so priority-1 providers win
    deterministically.
    """

    capability = await resolve_capability_record(capability_name, capability_repo)
    if not capability.enabled:
        raise CapabilityDisabledError(capability.name)

    providers = await _providers_for_request(
        capability=capability,
        provider_repo=provider_repo,
        provider_id=provider_id,
        provider_limit=provider_limit,
    )
    routing_request = _request_for_capability(request, capability)
    return select_stage12_provider(
        routing_request,
        providers,
        capability=capability,
        context=context,
        now=now,
    )


async def resolve_capability_record(
    capability_name: str,
    capability_repo: CapabilityRepositoryLike,
) -> Capability:
    """Resolve a capability by public name first, then by registry id."""

    capability = await capability_repo.get_by_name(capability_name)
    if capability is None:
        capability = await capability_repo.get(capability_name)
    if capability is None:
        raise CapabilityNotFoundError(capability_name)
    return capability


def select_stage12_provider(
    request: RoutingRequest,
    providers: Iterable[Provider],
    *,
    capability: Capability,
    context: PlanningContext | None = None,
    now: dt.datetime | None = None,
) -> Stage12Resolution:
    """Apply Stage 1+2 routing and select the highest-priority survivor."""

    observed_at = _observed_at(context=context, now=now)
    stage1 = filter_hard_constraints(request, providers, capability=capability)

    eliminated: list[RouteElimination] = [_stage1_elimination(item) for item in stage1.eliminated]

    eligible: list[Provider] = []
    for provider in cast(tuple[Provider, ...], stage1.passed):
        reasons = _stage2_reasons(provider, now=observed_at)
        if reasons:
            eliminated.append(
                RouteElimination(
                    provider_id=provider.id,
                    stage=2,
                    reason=reasons[0],
                    reasons=tuple(reasons),
                )
            )
            continue
        eligible.append(provider)

    if not eligible:
        raise NoHealthyProviderError(capability.name)

    ranked = tuple(sorted(eligible, key=_provider_priority_key))
    return Stage12Resolution(
        capability=capability,
        provider=ranked[0],
        eligible_providers=ranked,
        eliminated=tuple(eliminated),
    )


def _request_for_capability(
    request: RoutingRequest | None,
    capability: Capability,
) -> RoutingRequest:
    if request is None:
        return RoutingRequest(
            capability_name=capability.name,
            capability_id=capability.id,
        )
    return replace(
        request,
        capability_name=capability.name,
        capability_id=capability.id,
    )


async def _providers_for_request(
    *,
    capability: Capability,
    provider_repo: ProviderRepositoryLike,
    provider_id: str | None,
    provider_limit: int,
) -> tuple[Provider, ...]:
    if provider_id is not None:
        provider = await provider_repo.get(provider_id)
        if provider is None:
            raise ProviderNotFoundError(provider_id)
        return (provider,)

    providers = await provider_repo.list(
        capability_id=capability.id,
        enabled_only=True,
        limit=provider_limit,
    )
    return tuple(providers)


def _stage1_elimination(item: ConstraintResult) -> RouteElimination:
    reasons = tuple(ProviderEliminated(reason.value) for reason in item.reasons)
    if not reasons:
        raise ValueError("Stage 1 elimination must include at least one reason")
    return RouteElimination(
        provider_id=item.provider_id,
        stage=1,
        reason=reasons[0],
        reasons=reasons,
    )


def _stage2_reasons(provider: Provider, *, now: dt.datetime) -> list[ProviderEliminated]:
    reasons: list[ProviderEliminated] = []
    if not provider.enabled:
        reasons.append(ProviderEliminated.DISABLED)
    if provider.health_status.lower() != "healthy":
        reasons.append(ProviderEliminated.HEALTH_UNHEALTHY)
    if is_in_cooldown(provider, now=now):
        reasons.append(ProviderEliminated.HEALTH_COOLDOWN)
    return reasons


def _provider_priority_key(provider: Provider) -> tuple[int, str, str]:
    return (provider.priority, provider.name, provider.id)


def _observed_at(
    *,
    context: PlanningContext | None,
    now: dt.datetime | None,
) -> dt.datetime:
    if context is not None:
        if now is not None:
            raise ValueError("context and now are mutually exclusive")
        return context.now
    return _normalize_now(now)


def _normalize_now(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return PlanningContext.live().now
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must include timezone information")
    return value.astimezone(dt.UTC)


__all__ = [
    "CapabilityRepositoryLike",
    "ProviderRepositoryLike",
    "Stage12Resolution",
    "resolve_capability",
    "resolve_capability_record",
    "select_stage12_provider",
]
