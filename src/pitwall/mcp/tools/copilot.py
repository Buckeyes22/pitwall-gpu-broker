"""Broker Copilot MCP tool for proposal-only GitOps changes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pitwall.core.models import Capability, Provider
from pitwall.db import get_pool
from pitwall.db.repository import CapabilityRepository, ProviderRepository
from pitwall.gitops import (
    DesiredCapabilitySpec,
    DesiredProviderSpec,
    DesiredState,
    ReconcilePlan,
    build_reconcile_plan,
)
from pitwall.providers.drift import DriftFinding, DriftSeverity
from pitwall.recommendations.engine import (
    Recommendation,
    RecommendationEngine,
    ScorecardMetric,
)

_REF_PATTERN = r"(?P<ref>[a-zA-Z0-9_.:-]+)"
_DISABLE_PATTERNS = (
    re.compile(
        rf"\b(?:disable|deactivate|turn\s+off|take\s+offline)\s+provider\s+{_REF_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(rf"\bprovider\s+{_REF_PATTERN}\s+(?:disable|disabled|off|offline)\b", re.IGNORECASE),
)
_ENABLE_PATTERNS = (
    re.compile(rf"\b(?:enable|activate|turn\s+on)\s+provider\s+{_REF_PATTERN}\b", re.IGNORECASE),
    re.compile(rf"\bprovider\s+{_REF_PATTERN}\s+(?:enable|enabled|on|online)\b", re.IGNORECASE),
)
_PRIORITY_PATTERNS = (
    re.compile(
        rf"\bset\s+provider\s+{_REF_PATTERN}\s+priority\s+(?:to\s+)?(?P<priority>[0-9]+)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bprovider\s+{_REF_PATTERN}\s+priority\s+(?P<priority>[0-9]+)\b",
        re.IGNORECASE,
    ),
)
_PROVIDER_PATCH_FIELDS = frozenset(
    {
        "name",
        "provider_type",
        "runpod_endpoint_id",
        "runpod_template_id",
        "region",
        "cloud_type",
        "config",
        "priority",
        "enabled",
    }
)


@dataclass(frozen=True, slots=True)
class _ProviderProposal:
    provider_ref: str
    patch: dict[str, Any]
    rationale: str


async def pitwall_copilot_propose(
    intent: str,
    provider_ref: str | None = None,
    provider_enabled: bool | None = None,
    provider_priority: int | None = None,
    provider_patch: dict[str, Any] | None = None,
    scorecards: list[dict[str, Any]] | None = None,
    drift_findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a proposal-only GitOps plan/diff for an operator intent.

    The tool never applies the plan. It translates a constrained provider intent
    into a desired-state patch, runs the existing GitOps planner, and returns the
    resulting plan plus rationale for operator review.
    """

    cleaned_intent = intent.strip()
    if not cleaned_intent:
        raise ValueError("intent must be non-empty")

    recommendations = _recommendations_from_signals(
        scorecards=scorecards or [],
        drift_findings=drift_findings or [],
    )
    proposal = _proposal_from_explicit_args(
        provider_ref=provider_ref,
        provider_enabled=provider_enabled,
        provider_priority=provider_priority,
        provider_patch=provider_patch,
        intent=cleaned_intent,
    )
    if proposal is None:
        proposal = _proposal_from_intent(cleaned_intent)
    if proposal is None:
        proposal = _proposal_from_recommendations(recommendations)
    if proposal is None:
        raise ValueError(
            "unsupported copilot intent; supported provider proposals are enable, disable, "
            "and priority changes"
        )

    pool = await get_pool()
    capability_repo = CapabilityRepository(pool)
    provider_repo = ProviderRepository(pool)
    capabilities = await capability_repo.list(limit=1000, offset=0)
    providers = await provider_repo.list(limit=1000, offset=0)

    provider = _find_provider(providers, proposal.provider_ref)
    capability = _find_capability(capabilities, provider.capability_id)
    desired = _desired_state_for_provider_patch(
        capability=capability,
        provider=provider,
        patch=proposal.patch,
    )
    plan = build_reconcile_plan(
        desired,
        current_capabilities=[capability],
        current_providers=[provider],
    )
    plan_response = _plan_to_response(plan)

    return {
        "proposal_only": True,
        "applied": False,
        "intent": cleaned_intent,
        "rationale": [
            proposal.rationale,
            "Generated with pitwall.gitops.build_reconcile_plan; no apply path was invoked.",
        ],
        "recommendations": [recommendation.to_dict() for recommendation in recommendations],
        "desired_state": desired.model_dump(mode="json", by_alias=True),
        "plan": plan_response,
        "diff": plan_response,
    }


def _proposal_from_explicit_args(
    *,
    provider_ref: str | None,
    provider_enabled: bool | None,
    provider_priority: int | None,
    provider_patch: dict[str, Any] | None,
    intent: str,
) -> _ProviderProposal | None:
    patch = _normalize_provider_patch(provider_patch)
    if provider_enabled is not None:
        patch["enabled"] = provider_enabled
    if provider_priority is not None:
        patch["priority"] = provider_priority
    if not patch:
        return None
    if provider_ref is None or not provider_ref.strip():
        parsed = _proposal_from_intent(intent)
        if parsed is None:
            raise ValueError("provider_ref is required when provider patch fields are supplied")
        provider_ref = parsed.provider_ref
    return _ProviderProposal(
        provider_ref=provider_ref.strip(),
        patch=patch,
        rationale=f"Explicit provider patch requested for {provider_ref.strip()}: {sorted(patch)}.",
    )


def _proposal_from_intent(intent: str) -> _ProviderProposal | None:
    for pattern in _DISABLE_PATTERNS:
        match = pattern.search(intent)
        if match is not None:
            provider_ref = match.group("ref")
            return _ProviderProposal(
                provider_ref=provider_ref,
                patch={"enabled": False},
                rationale=f"Intent requests disable provider {provider_ref}.",
            )
    for pattern in _ENABLE_PATTERNS:
        match = pattern.search(intent)
        if match is not None:
            provider_ref = match.group("ref")
            return _ProviderProposal(
                provider_ref=provider_ref,
                patch={"enabled": True},
                rationale=f"Intent requests enable provider {provider_ref}.",
            )
    for pattern in _PRIORITY_PATTERNS:
        match = pattern.search(intent)
        if match is not None:
            provider_ref = match.group("ref")
            priority = int(match.group("priority"))
            return _ProviderProposal(
                provider_ref=provider_ref,
                patch={"priority": priority},
                rationale=f"Intent requests provider {provider_ref} priority {priority}.",
            )
    return None


def _proposal_from_recommendations(
    recommendations: list[Recommendation],
) -> _ProviderProposal | None:
    for recommendation in recommendations:
        if recommendation.target_provider_id is None:
            continue
        if recommendation.action == "disable_or_investigate_running_provider":
            return _ProviderProposal(
                provider_ref=recommendation.target_provider_id,
                patch={"enabled": False},
                rationale=(
                    "RecommendationEngine selected disable provider "
                    f"{recommendation.target_provider_id}: {recommendation.rationale}"
                ),
            )
        if recommendation.action == "reconcile_provider_enablement":
            return _ProviderProposal(
                provider_ref=recommendation.target_provider_id,
                patch={"enabled": True},
                rationale=(
                    "RecommendationEngine selected enable provider "
                    f"{recommendation.target_provider_id}: {recommendation.rationale}"
                ),
            )
    return None


def _recommendations_from_signals(
    *,
    scorecards: list[dict[str, Any]],
    drift_findings: list[dict[str, Any]],
) -> list[Recommendation]:
    if not scorecards and not drift_findings:
        return []
    return RecommendationEngine().recommend(
        scorecards=tuple(_scorecard_metric(item) for item in scorecards),
        drift_findings=tuple(_drift_finding(item) for item in drift_findings),
    )


def _scorecard_metric(item: Mapping[str, Any]) -> ScorecardMetric:
    return ScorecardMetric(
        capability_id=str(item["capability_id"]),
        provider_id=str(item["provider_id"]),
        dimension=str(item["dimension"]),
        score=Decimal(str(item["score"])),
        benchmark=Decimal(str(item["benchmark"])),
        message=str(item.get("message", "")),
    )


def _drift_finding(item: Mapping[str, Any]) -> DriftFinding:
    return DriftFinding(
        provider_id=str(item["provider_id"]),
        field=str(item["field"]),
        expected=item.get("expected"),
        observed=item.get("observed"),
        severity=DriftSeverity(str(item["severity"])),
        message=str(item.get("message", "")),
    )


def _normalize_provider_patch(provider_patch: dict[str, Any] | None) -> dict[str, Any]:
    if provider_patch is None:
        return {}
    unknown = sorted(set(provider_patch) - _PROVIDER_PATCH_FIELDS)
    if unknown:
        raise ValueError(f"unsupported provider_patch fields: {', '.join(unknown)}")
    return dict(provider_patch)


def _find_provider(providers: list[Provider], provider_ref: str) -> Provider:
    normalized = provider_ref.casefold()
    for provider in providers:
        if provider.id == provider_ref or provider.name == provider_ref:
            return provider
    for provider in providers:
        if provider.id.casefold() == normalized or provider.name.casefold() == normalized:
            return provider
    raise ValueError(f"provider not found for copilot proposal: {provider_ref}")


def _find_capability(capabilities: list[Capability], capability_id: str) -> Capability:
    for capability in capabilities:
        if capability.id == capability_id:
            return capability
    raise ValueError(f"capability not found for provider proposal: {capability_id}")


def _desired_state_for_provider_patch(
    *,
    capability: Capability,
    provider: Provider,
    patch: dict[str, Any],
) -> DesiredState:
    return DesiredState(
        capabilities=(_desired_capability(capability),),
        providers=(_desired_provider(provider, patch=patch),),
    )


def _desired_capability(capability: Capability) -> DesiredCapabilitySpec:
    return DesiredCapabilitySpec(
        id=capability.id,
        name=capability.name,
        version=capability.version,
        class_=capability.class_,
        description=capability.description,
        input_schema=capability.input_schema,
        output_schema=capability.output_schema,
        defaults=capability.defaults,
        cost_mode=capability.cost_mode,
        hints_supported=capability.hints_supported,
        enabled=capability.enabled,
        yaml_hash=capability.last_applied_yaml_hash or "copilot-proposal",
    )


def _desired_provider(provider: Provider, *, patch: dict[str, Any]) -> DesiredProviderSpec:
    values: dict[str, Any] = {
        "id": provider.id,
        "capability_id": provider.capability_id,
        "name": provider.name,
        "provider_type": provider.provider_type,
        "runpod_endpoint_id": provider.runpod_endpoint_id,
        "runpod_template_id": provider.runpod_template_id,
        "region": provider.region,
        "cloud_type": provider.cloud_type,
        "config": provider.config,
        "priority": provider.priority,
        "enabled": provider.enabled,
        "yaml_hash": provider.last_applied_yaml_hash or "copilot-proposal",
    }
    values.update(patch)
    return DesiredProviderSpec.model_validate(values)


def _plan_to_response(plan: ReconcilePlan) -> dict[str, Any]:
    return {
        "counts": plan.counts,
        "has_destructive_changes": plan.has_destructive_changes,
        "operations": [operation.model_dump(mode="json") for operation in plan.operations],
    }


__all__ = ["pitwall_copilot_propose"]
