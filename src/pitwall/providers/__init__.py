"""Provider plugin interface, registry, and built-in adapters."""

from pitwall.providers.drift import (
    DriftFinding,
    DriftSeverity,
    ProviderObservedState,
    detect_drift,
    observe_from_runpod_snapshot,
    observe_from_status_result,
)
from pitwall.providers.interface import (
    Provider,
    ProviderOperationContext,
    ProvisionRequest,
    ProvisionResult,
    ReconcileRequest,
    ReconcileResult,
    ResourceStatus,
    StatusRequest,
    StatusResult,
    TeardownRequest,
    TeardownResult,
)
from pitwall.providers.lambda_cloud import LambdaCloudCredentials, LambdaCloudProvider
from pitwall.providers.registry import (
    CredentialValidationError,
    DuplicateProviderError,
    ProviderNotRegisteredError,
    ProviderRegistry,
    ProviderRegistryError,
    create_default_registry,
    get_default_registry,
)
from pitwall.providers.runpod import RunPodCredentials, RunPodProvider
from pitwall.providers.together import (
    TogetherCredentials,
    TogetherInferenceResult,
    TogetherProvider,
    TogetherProviderError,
)
from pitwall.providers.vast import VastCredentials, VastProvider

__all__ = [
    "CredentialValidationError",
    "DuplicateProviderError",
    "Provider",
    "ProviderNotRegisteredError",
    "ProviderOperationContext",
    "ProviderRegistry",
    "ProviderRegistryError",
    "ProvisionRequest",
    "ProvisionResult",
    "ReconcileRequest",
    "ReconcileResult",
    "ResourceStatus",
    "RunPodCredentials",
    "RunPodProvider",
    "StatusRequest",
    "StatusResult",
    "TeardownRequest",
    "TeardownResult",
    "TogetherCredentials",
    "TogetherInferenceResult",
    "TogetherProvider",
    "TogetherProviderError",
    "create_default_registry",
    "get_default_registry",
    "VastCredentials",
    "VastProvider",
    "LambdaCloudCredentials",
    "LambdaCloudProvider",
    "DriftFinding",
    "DriftSeverity",
    "ProviderObservedState",
    "detect_drift",
    "observe_from_runpod_snapshot",
    "observe_from_status_result",
]
