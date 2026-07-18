"""Result types for the capability resolution path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pitwall.core.models import Provider


@dataclass(frozen=True, slots=True)
class ResolvedProvider:
    """Successfully resolved provider for a capability request."""

    provider: Provider
    is_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider.id,
            "provider_name": self.provider.name,
            "provider_type": self.provider.provider_type.value,
            "is_fallback": self.is_fallback,
        }


@dataclass(frozen=True, slots=True)
class ResolutionFailure:
    """Failed resolution with reason and context."""

    reason: str
    capability_name: str
    providers_tried: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "capability_name": self.capability_name,
            "providers_tried": self.providers_tried,
        }


ResolutionResult = ResolvedProvider | ResolutionFailure


__all__ = [
    "ResolvedProvider",
    "ResolutionFailure",
    "ResolutionResult",
]
