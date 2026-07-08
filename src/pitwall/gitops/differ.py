"""Deterministic GitOps diff and reconcile-plan generation."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import Field

from pitwall.core.enums import CapabilitySource
from pitwall.core.models import Capability, PitwallModel, Provider
from pitwall.gitops.schema import (
    DesiredCapabilitySpec,
    DesiredProviderSpec,
    DesiredState,
    GitOpsConfigError,
)

type Snapshot = dict[str, Any]

_CAPABILITY_FIELDS = (
    "id",
    "name",
    "version",
    "class",
    "description",
    "input_schema",
    "output_schema",
    "defaults",
    "cost_mode",
    "hints_supported",
    "enabled",
    "source",
    "last_applied_yaml_hash",
)
_PROVIDER_FIELDS = (
    "id",
    "capability_id",
    "name",
    "provider_type",
    "runpod_endpoint_id",
    "runpod_template_id",
    "region",
    "cloud_type",
    "config",
    "priority",
    "enabled",
    "source",
    "last_applied_yaml_hash",
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class PlanAction(StrEnum):
    """Reconcile operation kinds."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class PlanEntityType(StrEnum):
    """Registry entity kinds supported by GitOps."""

    CAPABILITY = "capability"
    PROVIDER = "provider"


class FieldChange(PitwallModel):
    """A single field-level difference."""

    current: Any
    desired: Any


class PlanOperation(PitwallModel):
    """One deterministic create, update, or delete operation."""

    action: PlanAction
    entity_type: PlanEntityType
    entity_id: str
    name: str
    current: Snapshot | None = None
    desired: Snapshot | None = None
    changes: dict[str, FieldChange] = Field(default_factory=dict)
    destructive: bool = False


class ReconcilePlan(PitwallModel):
    """Structured GitOps plan for capability/provider reconciliation."""

    operations: tuple[PlanOperation, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        """Return operation counts keyed by action value."""

        counts = {action.value: 0 for action in PlanAction}
        for operation in self.operations:
            counts[operation.action.value] += 1
        return counts

    @property
    def has_destructive_changes(self) -> bool:
        """Whether any operation requires explicit destructive approval."""

        return any(
            operation.action == PlanAction.DELETE or operation.destructive
            for operation in self.operations
        )


def build_reconcile_plan(
    desired: DesiredState,
    *,
    current_capabilities: list[Capability],
    current_providers: list[Provider],
) -> ReconcilePlan:
    """Diff desired YAML state against current registry models."""

    current_capabilities_by_id = {capability.id: capability for capability in current_capabilities}
    current_capabilities_by_name = {
        capability.name: capability for capability in current_capabilities
    }
    current_providers_by_name = {provider.name: provider for provider in current_providers}

    desired_capabilities = _desired_capability_snapshots(
        desired.capabilities,
        current_capabilities_by_name=current_capabilities_by_name,
    )
    desired_providers = _desired_provider_snapshots(
        desired.providers,
        desired_capabilities=desired_capabilities,
        current_capabilities_by_id=current_capabilities_by_id,
        current_capabilities_by_name=current_capabilities_by_name,
        current_providers_by_name=current_providers_by_name,
    )

    operations: list[PlanOperation] = []
    current_capability_snapshots = {
        capability.id: _capability_snapshot(capability) for capability in current_capabilities
    }
    current_provider_snapshots = {
        provider.id: _provider_snapshot(provider) for provider in current_providers
    }

    for capability_id in sorted(desired_capabilities):
        desired_snapshot = desired_capabilities[capability_id]
        current_snapshot = current_capability_snapshots.get(capability_id)
        if current_snapshot is None:
            operations.append(
                _operation(
                    PlanAction.CREATE,
                    PlanEntityType.CAPABILITY,
                    desired_snapshot,
                    current=None,
                    desired=desired_snapshot,
                )
            )
            continue
        changes = _changes(current_snapshot, desired_snapshot, _CAPABILITY_FIELDS)
        if changes:
            operations.append(
                _operation(
                    PlanAction.UPDATE,
                    PlanEntityType.CAPABILITY,
                    desired_snapshot,
                    current=current_snapshot,
                    desired=desired_snapshot,
                    changes=changes,
                )
            )

    for provider_id in sorted(desired_providers):
        desired_snapshot = desired_providers[provider_id]
        current_snapshot = current_provider_snapshots.get(provider_id)
        if current_snapshot is None:
            operations.append(
                _operation(
                    PlanAction.CREATE,
                    PlanEntityType.PROVIDER,
                    desired_snapshot,
                    current=None,
                    desired=desired_snapshot,
                )
            )
            continue
        changes = _changes(current_snapshot, desired_snapshot, _PROVIDER_FIELDS)
        if changes:
            operations.append(
                _operation(
                    PlanAction.UPDATE,
                    PlanEntityType.PROVIDER,
                    desired_snapshot,
                    current=current_snapshot,
                    desired=desired_snapshot,
                    changes=changes,
                )
            )

    for provider in sorted(current_providers, key=lambda item: item.id):
        if provider.id not in desired_providers and _is_yaml_owned(provider):
            current_snapshot = current_provider_snapshots[provider.id]
            operations.append(
                _operation(
                    PlanAction.DELETE,
                    PlanEntityType.PROVIDER,
                    current_snapshot,
                    current=current_snapshot,
                    desired=None,
                    destructive=True,
                )
            )

    for capability in sorted(current_capabilities, key=lambda item: item.id):
        if capability.id not in desired_capabilities and _is_yaml_owned(capability):
            current_snapshot = current_capability_snapshots[capability.id]
            operations.append(
                _operation(
                    PlanAction.DELETE,
                    PlanEntityType.CAPABILITY,
                    current_snapshot,
                    current=current_snapshot,
                    desired=None,
                    destructive=True,
                )
            )

    return ReconcilePlan(operations=tuple(operations))


def _desired_capability_snapshots(
    capabilities: tuple[DesiredCapabilitySpec, ...],
    *,
    current_capabilities_by_name: dict[str, Capability],
) -> dict[str, Snapshot]:
    snapshots: dict[str, Snapshot] = {}
    names: dict[str, str] = {}
    for capability in capabilities:
        current_by_name = current_capabilities_by_name.get(capability.name)
        if (
            capability.id is not None
            and current_by_name is not None
            and current_by_name.id != capability.id
        ):
            raise GitOpsConfigError(
                f"capability {capability.name!r} already exists as {current_by_name.id}; "
                f"cannot reconcile declared id {capability.id}"
            )
        capability_id = capability.id or (
            current_by_name.id
            if current_by_name is not None
            else _id_from_name("cap", capability.name)
        )
        snapshot: Snapshot = {
            "id": capability_id,
            "name": capability.name,
            "version": capability.version,
            "class": capability.class_.value,
            "description": capability.description,
            "input_schema": capability.input_schema,
            "output_schema": capability.output_schema,
            "defaults": capability.defaults.model_dump(mode="json"),
            "cost_mode": capability.cost_mode.value,
            "hints_supported": [hint.value for hint in capability.hints_supported],
            "enabled": capability.enabled,
            "source": CapabilitySource.YAML.value,
            "last_applied_yaml_hash": capability.yaml_hash,
        }
        _add_snapshot(
            snapshots,
            names,
            snapshot,
            entity_type=PlanEntityType.CAPABILITY,
        )
    return snapshots


def _desired_provider_snapshots(
    providers: tuple[DesiredProviderSpec, ...],
    *,
    desired_capabilities: dict[str, Snapshot],
    current_capabilities_by_id: dict[str, Capability],
    current_capabilities_by_name: dict[str, Capability],
    current_providers_by_name: dict[str, Provider],
) -> dict[str, Snapshot]:
    desired_capabilities_by_name = {
        snapshot["name"]: snapshot for snapshot in desired_capabilities.values()
    }
    snapshots: dict[str, Snapshot] = {}
    names: dict[str, str] = {}

    for provider in providers:
        current_by_name = current_providers_by_name.get(provider.name)
        if (
            provider.id is not None
            and current_by_name is not None
            and current_by_name.id != provider.id
        ):
            raise GitOpsConfigError(
                f"provider {provider.name!r} already exists as {current_by_name.id}; "
                f"cannot reconcile declared id {provider.id}"
            )
        provider_id = provider.id or (
            current_by_name.id
            if current_by_name is not None
            else _id_from_name("prov", provider.name)
        )
        capability_id = _resolve_capability_id(
            provider.capability_ref,
            desired_capabilities_by_id=desired_capabilities,
            desired_capabilities_by_name=desired_capabilities_by_name,
            current_capabilities_by_id=current_capabilities_by_id,
            current_capabilities_by_name=current_capabilities_by_name,
        )
        snapshot: Snapshot = {
            "id": provider_id,
            "capability_id": capability_id,
            "name": provider.name,
            "provider_type": provider.provider_type.value,
            "runpod_endpoint_id": provider.runpod_endpoint_id,
            "runpod_template_id": provider.runpod_template_id,
            "region": provider.region,
            "cloud_type": provider.cloud_type,
            "config": provider.config,
            "priority": provider.priority,
            "enabled": provider.enabled,
            "source": CapabilitySource.YAML.value,
            "last_applied_yaml_hash": provider.yaml_hash,
        }
        _add_snapshot(
            snapshots,
            names,
            snapshot,
            entity_type=PlanEntityType.PROVIDER,
        )
    return snapshots


def _capability_snapshot(capability: Capability) -> Snapshot:
    return {
        "id": capability.id,
        "name": capability.name,
        "version": capability.version,
        "class": capability.class_.value,
        "description": capability.description,
        "input_schema": capability.input_schema,
        "output_schema": capability.output_schema,
        "defaults": capability.defaults.model_dump(mode="json"),
        "cost_mode": capability.cost_mode.value,
        "hints_supported": [hint.value for hint in capability.hints_supported],
        "enabled": capability.enabled,
        "source": capability.source.value,
        "last_applied_yaml_hash": capability.last_applied_yaml_hash,
    }


def _provider_snapshot(provider: Provider) -> Snapshot:
    return {
        "id": provider.id,
        "capability_id": provider.capability_id,
        "name": provider.name,
        "provider_type": provider.provider_type.value,
        "runpod_endpoint_id": provider.runpod_endpoint_id,
        "runpod_template_id": provider.runpod_template_id,
        "region": provider.region,
        "cloud_type": provider.cloud_type,
        "config": provider.config,
        "priority": provider.priority,
        "enabled": provider.enabled,
        "source": provider.source.value,
        "last_applied_yaml_hash": provider.last_applied_yaml_hash,
    }


def _resolve_capability_id(
    ref: str,
    *,
    desired_capabilities_by_id: dict[str, Snapshot],
    desired_capabilities_by_name: dict[str, Snapshot],
    current_capabilities_by_id: dict[str, Capability],
    current_capabilities_by_name: dict[str, Capability],
) -> str:
    desired_by_id = desired_capabilities_by_id.get(ref)
    if desired_by_id is not None:
        return str(desired_by_id["id"])
    current_by_id = current_capabilities_by_id.get(ref)
    if current_by_id is not None:
        return current_by_id.id
    desired_by_name = desired_capabilities_by_name.get(ref)
    if desired_by_name is not None:
        return str(desired_by_name["id"])
    current_by_name = current_capabilities_by_name.get(ref)
    if current_by_name is not None:
        return current_by_name.id
    raise GitOpsConfigError(f"provider references unknown capability: {ref}")


def _changes(
    current: Snapshot,
    desired: Snapshot,
    fields: tuple[str, ...],
) -> dict[str, FieldChange]:
    changes: dict[str, FieldChange] = {}
    for field in fields:
        current_value = current.get(field)
        desired_value = desired.get(field)
        if current_value != desired_value:
            changes[field] = FieldChange(current=current_value, desired=desired_value)
    return changes


def _operation(
    action: PlanAction,
    entity_type: PlanEntityType,
    snapshot: Snapshot,
    *,
    current: Snapshot | None,
    desired: Snapshot | None,
    changes: dict[str, FieldChange] | None = None,
    destructive: bool = False,
) -> PlanOperation:
    return PlanOperation(
        action=action,
        entity_type=entity_type,
        entity_id=str(snapshot["id"]),
        name=str(snapshot["name"]),
        current=current,
        desired=desired,
        changes=changes or {},
        destructive=destructive,
    )


def _add_snapshot(
    snapshots: dict[str, Snapshot],
    names: dict[str, str],
    snapshot: Snapshot,
    *,
    entity_type: PlanEntityType,
) -> None:
    entity_id = str(snapshot["id"])
    name = str(snapshot["name"])
    if entity_id in snapshots:
        raise GitOpsConfigError(f"duplicate {entity_type.value} id: {entity_id}")
    existing_name_id = names.get(name)
    if existing_name_id is not None:
        raise GitOpsConfigError(
            f"duplicate {entity_type.value} name: {name} ({existing_name_id}, {entity_id})"
        )
    snapshots[entity_id] = snapshot
    names[name] = entity_id


def _id_from_name(prefix: str, name: str) -> str:
    slug = _SLUG_RE.sub("_", name.lower()).strip("_")
    if not slug:
        raise GitOpsConfigError(f"{prefix} id cannot be generated from an empty name")
    return f"{prefix}_{slug}"


def _is_yaml_owned(entity: Capability | Provider) -> bool:
    return entity.source == CapabilitySource.YAML or entity.last_applied_yaml_hash is not None


__all__ = [
    "FieldChange",
    "PlanAction",
    "PlanEntityType",
    "PlanOperation",
    "ReconcilePlan",
    "build_reconcile_plan",
]
