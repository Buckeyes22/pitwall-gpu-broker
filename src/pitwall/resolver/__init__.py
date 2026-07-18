"""Runtime capability resolver types.

The resolver maps consumer capability requests to concrete RunPod providers
using the four-stage routing algorithm (hard constraints, health gate,
hint-based ranking, fallback chain selection).
"""

from pitwall.resolver.exceptions import (
    CapabilityDisabledError,
    CapabilityNotFoundError,
    NoHealthyProviderError,
    ProviderExhaustedError,
    ProviderNotFoundError,
    ResolverError,
)
from pitwall.resolver.provider_urls import (
    lb_url,
    openai_base_url,
    provider_url,
    public_endpoint_url,
    queue_url,
)
from pitwall.resolver.result import (
    ResolutionFailure,
    ResolutionResult,
    ResolvedProvider,
)
from pitwall.resolver.service import (
    CapabilityRepositoryLike,
    ProviderRepositoryLike,
    Stage12Resolution,
    resolve_capability,
    resolve_capability_record,
    select_stage12_provider,
)

__all__ = [
    "CapabilityDisabledError",
    "CapabilityNotFoundError",
    "CapabilityRepositoryLike",
    "NoHealthyProviderError",
    "ProviderRepositoryLike",
    "ProviderExhaustedError",
    "ProviderNotFoundError",
    "ResolvedProvider",
    "ResolutionFailure",
    "ResolutionResult",
    "ResolverError",
    "Stage12Resolution",
    "lb_url",
    "openai_base_url",
    "provider_url",
    "public_endpoint_url",
    "queue_url",
    "resolve_capability",
    "resolve_capability_record",
    "select_stage12_provider",
]
