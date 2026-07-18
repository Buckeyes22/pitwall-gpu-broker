"""Resolver exception classes for the capability resolution path."""

from __future__ import annotations

from typing import Any


class ResolverError(RuntimeError):
    """Base class for all resolver exceptions."""

    error_code: str = "resolver_error"

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.error_code}


class CapabilityNotFoundError(ResolverError):
    """Raised when the requested capability name has no registered provider."""

    error_code = "capability_not_found"

    def __init__(self, capability_name: str) -> None:
        super().__init__(capability_name)
        self.capability_name = capability_name

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.error_code, "capability_name": self.capability_name}


class CapabilityDisabledError(ResolverError):
    """Raised when the capability exists but is currently disabled."""

    error_code = "capability_disabled"

    def __init__(self, capability_name: str) -> None:
        super().__init__(capability_name)
        self.capability_name = capability_name

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.error_code, "capability_name": self.capability_name}


class NoHealthyProviderError(ResolverError):
    """Raised when all providers for a capability are unhealthy or in cooldown."""

    error_code = "no_healthy_provider"

    def __init__(self, capability_name: str) -> None:
        super().__init__(capability_name)
        self.capability_name = capability_name

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.error_code, "capability_name": self.capability_name}


class ProviderNotFoundError(ResolverError):
    """Raised when a requested concrete provider does not exist."""

    error_code = "provider_not_found"

    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id)
        self.provider_id = provider_id

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.error_code, "provider_id": self.provider_id}


class ProviderExhaustedError(ResolverError):
    """Raised when all providers in the fallback chain have been tried and failed."""

    error_code = "provider_chain_exhausted"

    def __init__(self, capability_name: str, providers_tried: list[str]) -> None:
        super().__init__(capability_name)
        self.capability_name = capability_name
        self.providers_tried = providers_tried

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.error_code,
            "capability_name": self.capability_name,
            "providers_tried": self.providers_tried,
        }


__all__ = [
    "CapabilityDisabledError",
    "CapabilityNotFoundError",
    "NoHealthyProviderError",
    "ProviderExhaustedError",
    "ProviderNotFoundError",
    "ResolverError",
]
