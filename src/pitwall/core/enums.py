"""Registry enums for Pitwall."""

from __future__ import annotations

from enum import StrEnum


class RegistryPrefix(StrEnum):
    """Image registry prefix for selecting registry auth credentials.

    Used to route to the correct registry_auth_id when pulling images.
    See §14 in spec: "Registry-auth-id selected per image-ref prefix."
    """

    GHCR_IO = "ghcr.io"
    GITLAB_REGISTRY = "gitlab-registry."
    DOCKER_HUB = "registry.hub.docker.com"
    DOCKER_HUB_ALT = "index.docker.io"

    @classmethod
    def from_image_ref(cls, image_ref: str) -> RegistryPrefix | None:
        """Return the RegistryPrefix matching the given image ref, or None if unrecognized."""
        if image_ref.startswith(cls.GHCR_IO):
            return cls.GHCR_IO
        if cls.GITLAB_REGISTRY in image_ref:
            return cls.GITLAB_REGISTRY
        if image_ref.startswith(cls.DOCKER_HUB):
            return cls.DOCKER_HUB
        if image_ref.startswith(cls.DOCKER_HUB_ALT):
            return cls.DOCKER_HUB_ALT
        return None


class CapabilityClass(StrEnum):
    """Capability classes supported by the v1 runtime registry."""

    EMBEDDING = "embedding"
    RERANK = "rerank"
    LLM = "llm"
    VISION = "vision"
    TRANSCRIBE = "transcribe"
    GPU_LEASE = "gpu_lease"
    CUSTOM = "custom"


class CostMode(StrEnum):
    """Cost estimator selection for admission and reconciliation."""

    PER_SECOND = "per_second"
    PER_REQUEST = "per_request"
    PER_TOKEN = "per_token"


class CapabilitySource(StrEnum):
    """Source of capability registration."""

    API = "api"
    MCP = "mcp"
    YAML = "yaml"


class ResultDelivery(StrEnum):
    """Requested result delivery path for a capability default."""

    SYNC = "sync"
    ASYNC = "async"


class CapabilityHint(StrEnum):
    """Hints that a capability can use when ranking providers."""

    LATENCY_SENSITIVE = "latency_sensitive"
    COST_SENSITIVE = "cost_sensitive"
    REGION_PREFERENCE = "region_preference"


class WorkloadState(StrEnum):
    """Persisted workload lifecycle states."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ProviderType(StrEnum):
    """RunPod provider surfaces supported by Pitwall."""

    SERVERLESS_QUEUE = "serverless_queue"
    SERVERLESS_LB = "serverless_lb"
    PUBLIC_ENDPOINT = "public_endpoint"
    POD_LEASE = "pod_lease"


class LeaseState(StrEnum):
    """Persisted pod lease lifecycle states."""

    CREATING = "creating"
    WAITING_RUNTIME = "waiting_runtime"
    WAITING_PROBE = "waiting_probe"
    ACTIVE = "active"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    EXPIRED = "expired"


class LeaseRenewalPolicy(StrEnum):
    """Renewal modes for pod leases."""

    MANUAL = "manual"
