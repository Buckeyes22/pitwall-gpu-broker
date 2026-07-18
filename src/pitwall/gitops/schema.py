"""Versioned desired-state schema for Pitwall GitOps registry config."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, ValidationError, model_validator

from pitwall.core.enums import CapabilityClass, CapabilityHint, CostMode, ProviderType
from pitwall.core.models import CapabilityDefaults, JsonObject, PitwallModel
from pitwall.seed import SeedValidationError, load_seed_documents

GITOPS_API_VERSION: Literal["pitwall.dev/v1"] = "pitwall.dev/v1"


class GitOpsConfigError(ValueError):
    """Raised when desired-state YAML cannot be loaded or reconciled safely."""


class DesiredCapabilitySpec(PitwallModel):
    """Desired GitOps declaration for one capability."""

    id: str | None = None
    name: str = Field(min_length=1)
    version: str = Field(default="1.0.0", min_length=1)
    class_: CapabilityClass = Field(
        validation_alias=AliasChoices("class", "class_", "capability_class"),
        serialization_alias="class",
    )
    description: str | None = None
    input_schema: JsonObject = Field(default_factory=dict)
    output_schema: JsonObject = Field(default_factory=dict)
    defaults: CapabilityDefaults = Field(default_factory=CapabilityDefaults)
    cost_mode: CostMode = CostMode.PER_SECOND
    hints_supported: list[CapabilityHint] = Field(default_factory=list)
    enabled: bool = True
    yaml_hash: str = "manual"


class DesiredProviderSpec(PitwallModel):
    """Desired GitOps declaration for one provider."""

    id: str | None = None
    capability: str | None = None
    capability_id: str | None = None
    capability_name: str | None = None
    name: str = Field(min_length=1)
    provider_type: ProviderType = Field(validation_alias=AliasChoices("provider_type", "type"))
    runpod_endpoint_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("runpod_endpoint_id", "endpoint_id"),
    )
    runpod_template_id: str | None = None
    region: str | None = None
    cloud_type: str | None = None
    config: JsonObject = Field(default_factory=dict)
    priority: int = Field(default=0, ge=0)
    enabled: bool = True
    yaml_hash: str = "manual"

    @model_validator(mode="after")
    def _require_single_capability_reference(self) -> DesiredProviderSpec:
        refs = [
            ref
            for ref in (self.capability, self.capability_id, self.capability_name)
            if ref is not None
        ]
        if not refs:
            raise ValueError("provider must include capability, capability_id, or capability_name")
        if len(refs) > 1:
            raise ValueError(
                "provider must include exactly one of capability, capability_id, or capability_name"
            )
        return self

    @property
    def capability_ref(self) -> str:
        """Return the configured capability reference."""

        ref = self.capability or self.capability_id or self.capability_name
        if ref is None:
            raise GitOpsConfigError(
                "provider must include capability, capability_id, or capability_name"
            )
        return ref


class DesiredStateDocument(PitwallModel):
    """One versioned desired-state YAML document."""

    api_version: Literal["pitwall.dev/v1"] = Field(
        validation_alias=AliasChoices("apiVersion", "api_version")
    )
    capabilities: list[DesiredCapabilitySpec] = Field(default_factory=list)
    providers: list[DesiredProviderSpec] = Field(default_factory=list)


class DesiredState(PitwallModel):
    """Combined desired state loaded from one or more versioned YAML files."""

    api_version: Literal["pitwall.dev/v1"] = GITOPS_API_VERSION
    capabilities: tuple[DesiredCapabilitySpec, ...] = ()
    providers: tuple[DesiredProviderSpec, ...] = ()

    @model_validator(mode="after")
    def _reject_duplicate_declared_keys(self) -> DesiredState:
        _reject_duplicates(
            "capability name",
            [capability.name for capability in self.capabilities],
        )
        _reject_duplicates(
            "provider name",
            [provider.name for provider in self.providers],
        )
        _reject_duplicates(
            "capability id",
            [capability.id for capability in self.capabilities if capability.id is not None],
        )
        _reject_duplicates(
            "provider id",
            [provider.id for provider in self.providers if provider.id is not None],
        )
        return self


def load_desired_state(paths: Sequence[str | Path]) -> DesiredState:
    """Load one or more versioned desired-state YAML/JSON files."""

    try:
        documents = load_seed_documents(paths)
    except SeedValidationError as exc:
        raise GitOpsConfigError(str(exc)) from exc

    capabilities: list[DesiredCapabilitySpec] = []
    providers: list[DesiredProviderSpec] = []
    api_version: Literal["pitwall.dev/v1"] | None = None

    for document in documents:
        try:
            parsed = DesiredStateDocument.model_validate(document.payload)
        except ValidationError as exc:
            raise GitOpsConfigError(f"{document.path}: {exc}") from exc

        if api_version is None:
            api_version = parsed.api_version
        elif parsed.api_version != api_version:
            raise GitOpsConfigError(
                f"{document.path}: apiVersion {parsed.api_version!r} does not match {api_version!r}"
            )

        capabilities.extend(
            capability.model_copy(update={"yaml_hash": document.content_hash})
            for capability in parsed.capabilities
        )
        providers.extend(
            provider.model_copy(update={"yaml_hash": document.content_hash})
            for provider in parsed.providers
        )

    try:
        return DesiredState(
            api_version=api_version or GITOPS_API_VERSION,
            capabilities=tuple(capabilities),
            providers=tuple(providers),
        )
    except ValidationError as exc:
        raise GitOpsConfigError(str(exc)) from exc


def _reject_duplicates(label: str, values: Sequence[str]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        rendered = ", ".join(sorted(duplicates))
        raise ValueError(f"duplicate {label}: {rendered}")


__all__ = [
    "GITOPS_API_VERSION",
    "DesiredCapabilitySpec",
    "DesiredProviderSpec",
    "DesiredState",
    "DesiredStateDocument",
    "GitOpsConfigError",
    "load_desired_state",
]
