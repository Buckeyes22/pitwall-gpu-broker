"""Provider Wave-2 feasibility analysis.

This module provides typed data structures for assessing Paperspace, Modal, and
CoreWeave against the Provider plugin interface. No live integrations
are implemented here; the structures capture interface-fit notes, auth shape,
pricing alignment, and effort/fit ratings for a feasibility spike.

Each candidate entry is a one-stop struct consumed by the SDLC doc generator and
by the hermetic test suite in tests/providers/test_wave2_feasibility.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EffortRating(StrEnum):
    """Relative implementation effort on a 1-5 scale (1=trivial, 5=major)."""

    TRIVIAL = "1_trivial"
    LOW = "2_low"
    MEDIUM = "3_medium"
    HIGH = "4_high"
    MAJOR = "5_major"


class FitRating(StrEnum):
    """Conceptual fit of the candidate to the existing Provider interface.

    Scores reflect alignment with the four Wave-1 adapters (RunPod, Vast,
    Lambda Cloud, Together) in: auth model, pricing shape, lease lifecycle
    coverage, and API ergonomics for a Python/httpx client.
    """

    EXCELLENT = "5_excellent"
    GOOD = "4_good"
    MODERATE = "3_moderate"
    POOR = "2_poor"
    INCOMPATIBLE = "1_incompatible"


class AuthType(StrEnum):
    """Authentication mechanism shape."""

    HEADER_BEARER = "header_bearer"
    API_KEY_QUERY = "api_key_query"
    OAUTH2 = "oauth2"
    AWS_V4 = "aws_v4"


class LeaseModel(StrEnum):
    """Resource leasing model supported by the provider."""

    GPU_LEASE_SECOND = "gpu_lease_second"
    GPU_LEASE_HOUR = "gpu_lease_hour"
    SERVERLESS_INVOCATION = "serverless_invocation"
    KUBERNETES_POD = "kubernetes_pod"
    BARE_METAL = "bare_metal"


class PricingAlignment(StrEnum):
    """How naturally the provider's pricing maps onto TaggedPricingModel variants."""

    DIRECT = "direct"
    CONVERSION_REQUIRED = "conversion_required"
    NOBLE = "no_direct_match"


@dataclass(frozen=True, slots=True)
class PricingFit:
    """Pricing shape notes for one candidate."""

    alignment: PricingAlignment
    compatible_kinds: tuple[str, ...]
    notes: str


@dataclass(frozen=True, slots=True)
class AuthFit:
    """Auth mechanism notes for one candidate."""

    auth_type: AuthType
    header_bearer_compatible: bool
    notes: str


@dataclass(frozen=True, slots=True)
class InterfaceFit:
    """Per-method interface gap analysis."""

    provision_gaps: tuple[str, ...]
    status_gaps: tuple[str, ...]
    reconcile_gaps: tuple[str, ...]
    teardown_gaps: tuple[str, ...]
    notes: str


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    """Full feasibility record for one Wave-2 provider candidate."""

    candidate_id: str
    name: str
    url: str
    auth_fit: AuthFit
    pricing_fit: PricingFit
    lease_model: LeaseModel
    interface_fit: InterfaceFit
    effort_rating: EffortRating
    fit_rating: FitRating
    summary: str
    blocking_issues: tuple[str, ...] = field(default_factory=tuple)


PAPERSPACE_AUTH = AuthFit(
    auth_type=AuthType.HEADER_BEARER,
    header_bearer_compatible=True,
    notes=(
        "Paperspace Core API uses Authorization: Bearer {api_key} header. "
        "API key passed via x-api-key header in some endpoints; verify bearer "
        "parity. No OAuth2 or AWS SIG observed in public docs."
    ),
)

PAPERSPACE_PRICING = PricingFit(
    alignment=PricingAlignment.CONVERSION_REQUIRED,
    compatible_kinds=("per_second", "per_vm_second"),
    notes=(
        "Paperspace GPU Cloud bills per-second on on-demand instances. "
        "Hourly rate is published; dividing by 3600 yields per-second "
        "equivalent, matching PerSecondPricing / PerVmSecondPricing. "
        "Spot/preemptible instances support bid-price mechanism that maps "
        "to PerSecondPricing.bid_rate_per_second."
    ),
)

PAPERSPACE_INTERFACE = InterfaceFit(
    provision_gaps=(
        "No official public REST API for instance launch confirmed in public docs;",
        "Gradient SDK (Python) is the primary control plane;",
        "No surfaced 'offer/ask' market model like Vast — flat on-demand pricing;",
        "Lease lifecycle (create/wait/runtime/probe/active) needs custom mapping.",
    ),
    status_gaps=(
        "Status field names and lifecycle states differ from Lambda/Vast patterns;",
        "No confirmed /instances/{id} GET in public Core API;",
    ),
    reconcile_gaps=(
        "Bulk listing endpoint not confirmed in public Core API docs;",
        "No surfaced preemption signal like Lambda/Vast.",
    ),
    teardown_gaps=(
        "Terminate endpoint confirmed but path/layout unconfirmed;",
        "runpod_pod_id column reuse for Paperspace VM ids needs migration plan.",
    ),
    notes=(
        "Primary control plane is the Gradient SDK, not a public REST API. "
        "The Provider interface expects a REST adapter. Bridging through the "
        "Python SDK would be a layer mismatch unless Paperspace exposes an "
        "undocumented/undiscovered public REST API."
    ),
)

PAPERSPACE = CandidateAssessment(
    candidate_id="paperspace",
    name="Paperspace",
    url="https://www.paperspace.com/core",
    auth_fit=PAPERSPACE_AUTH,
    pricing_fit=PAPERSPACE_PRICING,
    lease_model=LeaseModel.GPU_LEASE_SECOND,
    interface_fit=PAPERSPACE_INTERFACE,
    effort_rating=EffortRating.HIGH,
    fit_rating=FitRating.MODERATE,
    summary=(
        "Paperspace GPU Cloud is a solid conceptual fit (per-second GPU lease, "
        "bearer auth). The primary blocker is the absence of a confirmed public "
        "REST API — the Gradient SDK is the documented control plane, which "
        "does not align with the REST/httpx adapter pattern used by all four "
        "Wave-1 providers. If a REST API exists and is stable, effort drops "
        "to MEDIUM. Pricing alignment is straightforward."
    ),
    blocking_issues=(
        "No confirmed public REST API for instance lifecycle management;",
        "Gradient SDK is the primary control plane and does not map to the "
        "Provider protocol REST-adapter pattern;",
        "API stability / deprecation risk unconfirmed.",
    ),
)

MODAL_AUTH = AuthFit(
    auth_type=AuthType.HEADER_BEARER,
    header_bearer_compatible=True,
    notes=(
        "Modal uses API tokens passed as x-modal-api-key header or "
        "Authorization: Bearer header. No OAuth2 or container-based "
        "credential flows observed in public docs."
    ),
)

MODAL_PRICING = PricingFit(
    alignment=PricingAlignment.CONVERSION_REQUIRED,
    compatible_kinds=("per_second", "per_request", "per_token"),
    notes=(
        "Modal bills compute per-second (container wall-time) and supports "
        "per-invocation and per-token pricing for certain endpoints. "
        "Wall-time seconds map to PerSecondPricing; per-invocation maps "
        "to PerRequestPricing; token-based endpoints (when exposed) map "
        "to PerTokenPricing. Modal's dynamic compute model is closer to "
        "serverless than to a persistent GPU lease."
    ),
)

MODAL_INTERFACE = InterfaceFit(
    provision_gaps=(
        "Modal's 'app' and 'stub' programming model does not map 1:1 to "
        "ProvisionRequest — there is no persistent VM concept;",
        "Provision maps to Modal's @stub Decorator + .spawn() or .deploy();",
        "External ID concept differs: Modal uses 'call_id' / 'function_call_id', "
        "not a VM/instance id.",
    ),
    status_gaps=(
        "Status is implicit — a Modal function call is either running or "
        "completed/failed; no persistent 'running' resource to poll;",
        "No bulk list resources endpoint — Modal's serverless model does not expose a fleet view.",
    ),
    reconcile_gaps=(
        "No reconcile equivalent — Modal's serverless model is stateless;",
        "No preemption signal to handle like Vast/Lambda.",
    ),
    teardown_gaps=(
        "Teardown is implicit — serverless calls complete or are abandoned;",
        "No persistent lease to terminate;",
        "Modal credits/subscriptions are account-level, not per-lease.",
    ),
    notes=(
        "Modal's programming-model architecture is fundamentally serverless, "
        "not a persistent GPU lease broker. It does not expose a fleet view, "
        "status polling, or explicit teardown. Attempting to map it to the "
        "Provider protocol would require significant conceptual compression "
        "and likely a Modal-specific subclass rather than a direct adapter."
    ),
)

MODAL = CandidateAssessment(
    candidate_id="modal",
    name="Modal",
    url="https://modal.com",
    auth_fit=MODAL_AUTH,
    pricing_fit=MODAL_PRICING,
    lease_model=LeaseModel.SERVERLESS_INVOCATION,
    interface_fit=MODAL_INTERFACE,
    effort_rating=EffortRating.MAJOR,
    fit_rating=FitRating.POOR,
    summary=(
        "Modal's serverless, programming-model-first architecture is a "
        "poor structural fit for the Provider protocol, which assumes a "
        "persistent GPU lease with explicit provision/status/reconcile/"
        "teardown lifecycle. Auth (bearer) and pricing (per-second/"
        "per-request) are individually compatible, but the conceptual "
        "gap in the lifecycle model is large. A Modal-specific "
        "abstraction layer outside the Provider plugin seam would be "
        "more appropriate than a direct adapter."
    ),
    blocking_issues=(
        "Modal's serverless model has no persistent resources to provision, "
        "status-poll, or teardown; the core Provider lifecycle assumptions "
        "do not apply;",
        "No fleet-level visibility — Modal does not surface a list-instances "
        "or reconcile endpoint;",
        "External-id mapping is call-based, not instance-based.",
    ),
)

COREWEAVE_AUTH = AuthFit(
    auth_type=AuthType.HEADER_BEARER,
    header_bearer_compatible=True,
    notes=(
        "CoreWeave Cloud uses API tokens via Authorization: Bearer header. "
        "Kubernetes cluster credentials (kubeconfig) are a separate auth "
        "plane not relevant to a REST adapter. No OAuth2 or AWS SIG "
        "observed for the REST API."
    ),
)

COREWEAVE_PRICING = PricingFit(
    alignment=PricingAlignment.DIRECT,
    compatible_kinds=("per_second", "per_vm_second"),
    notes=(
        "CoreWeave publishes hourly GPU instance rates. Dividing by 3600 "
        "yields a per-second rate — direct mapping to PerSecondPricing. "
        "CoreWeave's preemptible/spot instances have a bid price that maps "
        "to PerSecondPricing.bid_rate_per_second. No per-token or "
        "per-request model in core GPU compute."
    ),
)

COREWEAVE_INTERFACE = InterfaceFit(
    provision_gaps=(
        "CoreWeave's primary control plane is Kubernetes — provision "
        "means creating a Pod or Deployment via kubectl/API;",
        "CoreWeave REST API (cloud.coreweave.com) may offer a simpler "
        "instance lifecycle API — needs confirmation;",
        "No surfaced offer/market model — flat on-demand pricing;",
        "Lease TTL/expiry needs explicit management (Kubernetes Pod does not auto-expire).",
    ),
    status_gaps=(
        "Pod status polling maps naturally to ResourceStatus;",
        "Kubernetes watch events could supplement polling for reconcile;",
        "Status field name normalization (conditions vs status vs state) "
        "needed vs Lambda/Vast patterns.",
    ),
    reconcile_gaps=(
        "Kubernetes label selectors + GET /pods can implement fleet reconciliation naturally;",
        "Preemption via Pod.preemptionPolicy maps to the preemption "
        "signal pattern used in Vast/Lambda;",
        "No bulk REST listing endpoint confirmed for cloud.coreweave.com API.",
    ),
    teardown_gaps=(
        "DELETE /pods or Deployment scales to zero is the natural teardown;",
        "TTL Policy + finalizers handle lease expiry cleanly.",
    ),
    notes=(
        "CoreWeave is Kubernetes-native, which is both a strength and a "
        "gap. The Provider protocol is REST/httpx-based; Kubernetes API "
        "uses a different client (official kubernetes-python client or "
        "kubernetes-asyncio). A thin REST adapter over cloud.coreweave.com "
        "would align with the existing pattern, but if CoreWeave's "
        "recommended control plane is kubectl/kubeconfig, a dedicated "
        "KubernetesRuntimeProvider outside the REST adapter seam would "
        "be more idiomatic. The cloud REST API confirmation is the "
        "critical unknown."
    ),
)

COREWEAVE = CandidateAssessment(
    candidate_id="coreweave",
    name="CoreWeave",
    url="https://www.coreweave.com/cloud",
    auth_fit=COREWEAVE_AUTH,
    pricing_fit=COREWEAVE_PRICING,
    lease_model=LeaseModel.KUBERNETES_POD,
    interface_fit=COREWEAVE_INTERFACE,
    effort_rating=EffortRating.HIGH,
    fit_rating=FitRating.MODERATE,
    summary=(
        "CoreWeave is a Kubernetes-first GPU cloud with bearer auth and "
        "per-second billing that maps cleanly to PerSecondPricing. The "
        "interface gap is the control plane: CoreWeave's idiomatic "
        "control path is kubectl/kubeconfig, not a REST adapter over "
        "cloud.coreweave.com. If a stable REST API exists for instance "
        "lifecycle, effort drops to MEDIUM and fit becomes GOOD. "
        "Pricing alignment is excellent."
    ),
    blocking_issues=(
        "CoreWeave's recommended control plane is Kubernetes (kubectl/API), "
        "not a REST adapter — would require a dedicated KubernetesRuntimeProvider "
        "seam rather than a direct Provider adapter;",
        "No confirmed stable REST API for instance lifecycle at cloud.coreweave.com;",
        "Lease expiry management (TTL finalizers) requires explicit Kubernetes "
        "runtime integration.",
    ),
)

WAVE2_CANDIDATES: tuple[CandidateAssessment, ...] = (PAPERSPACE, MODAL, COREWEAVE)


def all_candidates() -> tuple[CandidateAssessment, ...]:
    """Return all Wave-2 candidate assessments in priority order."""
    return WAVE2_CANDIDATES


def candidate_by_id(candidate_id: str) -> CandidateAssessment | None:
    """Return the candidate assessment for *candidate_id*, or None."""
    for candidate in WAVE2_CANDIDATES:
        if candidate.candidate_id == candidate_id:
            return candidate
    return None


__all__ = [
    "AuthFit",
    "AuthType",
    "CandidateAssessment",
    "COREWEAVE",
    "EffortRating",
    "FitRating",
    "InterfaceFit",
    "LeaseModel",
    "MODAL",
    "PAPERSPACE",
    "PricingAlignment",
    "PricingFit",
    "all_candidates",
    "candidate_by_id",
]
