"""Apply GitOps reconcile plans through existing registry repositories."""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable
from typing import Any, Protocol

from pitwall.core.enums import (
    CapabilityClass,
    CapabilityHint,
    CapabilitySource,
    CostMode,
    ProviderType,
)
from pitwall.core.models import Capability, CapabilityDefaults, JsonObject, PitwallModel, Provider
from pitwall.db.repository import insert_audit
from pitwall.gitops.differ import PlanAction, PlanEntityType, PlanOperation, ReconcilePlan, Snapshot
from pitwall.gitops.schema import GitOpsConfigError


class GitOpsDestructiveChangeError(RuntimeError):
    """Raised when a plan with deletes is applied without explicit approval."""


class CapabilityRepositoryLike(Protocol):
    """Repository surface used by GitOps for capabilities."""

    async def get(self, capability_id: str) -> Capability | None: ...

    async def create(self, cap: Capability) -> Capability: ...

    async def enable(self, capability_id: str) -> Capability | None: ...

    async def disable(self, capability_id: str) -> Capability | None: ...


class ProviderRepositoryLike(Protocol):
    """Repository surface used by GitOps for providers."""

    async def get(self, provider_id: str) -> Provider | None: ...

    async def create(self, provider: Provider) -> Provider: ...

    async def enable(self, provider_id: str) -> Provider | None: ...

    async def disable(self, provider_id: str) -> Provider | None: ...


class AuditWriter(Protocol):
    """Callable matching :func:`pitwall.db.repository.insert_audit`."""

    def __call__(
        self,
        pool: Any,
        *,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        old_value: JsonObject | None = None,
        new_value: JsonObject | None = None,
        change_reason: str | None = None,
    ) -> Awaitable[object]: ...


class GitOpsApplyResult(PitwallModel):
    """Result from dry-running or applying a reconcile plan."""

    plan: ReconcilePlan
    dry_run: bool
    applied: bool
    applied_operations: tuple[str, ...] = ()


async def apply_plan(
    plan: ReconcilePlan,
    *,
    capability_repo: CapabilityRepositoryLike,
    provider_repo: ProviderRepositoryLike,
    pool: Any | None = None,
    dry_run: bool = True,
    allow_delete: bool = False,
    actor: str = "gitops:admin",
    change_reason: str | None = None,
    audit_writer: AuditWriter = insert_audit,
) -> GitOpsApplyResult:
    """Apply a reconcile plan; defaults to dry-run and refuses deletes without a flag."""

    if dry_run:
        return GitOpsApplyResult(plan=plan, dry_run=True, applied=False)

    if plan.has_destructive_changes and not allow_delete:
        raise GitOpsDestructiveChangeError(
            "plan contains delete operations; pass allow_delete=True to apply"
        )
    if pool is None:
        raise GitOpsConfigError("pool is required when applying a GitOps plan")

    applied: list[str] = []
    for operation in plan.operations:
        await _apply_operation(
            operation, capability_repo=capability_repo, provider_repo=provider_repo
        )
        await audit_writer(
            pool,
            actor=actor,
            action=f"gitops:{operation.action.value}",
            entity_type=operation.entity_type.value,
            entity_id=operation.entity_id,
            old_value=operation.current,
            new_value=operation.desired,
            change_reason=change_reason,
        )
        applied.append(
            f"{operation.action.value}:{operation.entity_type.value}:{operation.entity_id}"
        )

    return GitOpsApplyResult(
        plan=plan,
        dry_run=False,
        applied=True,
        applied_operations=tuple(applied),
    )


async def _apply_operation(
    operation: PlanOperation,
    *,
    capability_repo: CapabilityRepositoryLike,
    provider_repo: ProviderRepositoryLike,
) -> None:
    if operation.entity_type == PlanEntityType.CAPABILITY:
        await _apply_capability_operation(operation, capability_repo)
        return
    await _apply_provider_operation(operation, provider_repo)


async def _apply_capability_operation(
    operation: PlanOperation,
    repo: CapabilityRepositoryLike,
) -> None:
    if operation.action == PlanAction.DELETE:
        await _disable_capability(repo, operation.entity_id)
        return

    desired = _require_desired(operation)
    now = dt.datetime.now(dt.UTC)
    existing = await repo.get(operation.entity_id)
    if operation.action == PlanAction.UPDATE and existing is None:
        raise GitOpsConfigError(f"cannot update missing capability: {operation.entity_id}")

    should_upsert = operation.action == PlanAction.CREATE or _has_non_enabled_change(operation)
    if should_upsert:
        capability = _capability_from_snapshot(desired, now=now, existing=existing)
        await repo.create(capability)

    desired_enabled = bool(desired["enabled"])
    current_enabled = existing.enabled if existing is not None else True
    if desired_enabled != current_enabled:
        if desired_enabled:
            await _enable_capability(repo, operation.entity_id)
        else:
            await _disable_capability(repo, operation.entity_id)


async def _apply_provider_operation(
    operation: PlanOperation,
    repo: ProviderRepositoryLike,
) -> None:
    if operation.action == PlanAction.DELETE:
        await _disable_provider(repo, operation.entity_id)
        return

    desired = _require_desired(operation)
    now = dt.datetime.now(dt.UTC)
    existing = await repo.get(operation.entity_id)
    if operation.action == PlanAction.UPDATE and existing is None:
        raise GitOpsConfigError(f"cannot update missing provider: {operation.entity_id}")

    should_upsert = operation.action == PlanAction.CREATE or _has_non_enabled_change(operation)
    if should_upsert:
        provider = _provider_from_snapshot(desired, now=now, existing=existing)
        await repo.create(provider)

    desired_enabled = bool(desired["enabled"])
    current_enabled = existing.enabled if existing is not None else True
    if desired_enabled != current_enabled:
        if desired_enabled:
            await _enable_provider(repo, operation.entity_id)
        else:
            await _disable_provider(repo, operation.entity_id)


def _capability_from_snapshot(
    snapshot: Snapshot,
    *,
    now: dt.datetime,
    existing: Capability | None,
) -> Capability:
    return Capability(
        id=str(snapshot["id"]),
        name=str(snapshot["name"]),
        version=str(snapshot["version"]),
        class_=CapabilityClass(str(snapshot["class"])),
        description=_optional_string(snapshot.get("description")),
        input_schema=_json_object(snapshot["input_schema"], "capability.input_schema"),
        output_schema=_json_object(snapshot["output_schema"], "capability.output_schema"),
        defaults=CapabilityDefaults.model_validate(snapshot["defaults"]),
        cost_mode=CostMode(str(snapshot["cost_mode"])),
        hints_supported=[
            CapabilityHint(str(hint)) for hint in _list_value(snapshot["hints_supported"])
        ],
        source=CapabilitySource(str(snapshot["source"])),
        last_applied_yaml_hash=_optional_string(snapshot.get("last_applied_yaml_hash")),
        enabled=bool(snapshot["enabled"]),
        created_at=existing.created_at if existing is not None else now,
        updated_at=now,
    )


def _provider_from_snapshot(
    snapshot: Snapshot,
    *,
    now: dt.datetime,
    existing: Provider | None,
) -> Provider:
    return Provider(
        id=str(snapshot["id"]),
        capability_id=str(snapshot["capability_id"]),
        name=str(snapshot["name"]),
        provider_type=ProviderType(str(snapshot["provider_type"])),
        runpod_endpoint_id=_optional_string(snapshot.get("runpod_endpoint_id")),
        runpod_template_id=_optional_string(snapshot.get("runpod_template_id")),
        region=_optional_string(snapshot.get("region")),
        cloud_type=_optional_string(snapshot.get("cloud_type")),
        config=_json_object(snapshot["config"], "provider.config"),
        priority=int(snapshot["priority"]),
        enabled=bool(snapshot["enabled"]),
        health_status=existing.health_status if existing is not None else "unknown",
        consecutive_failures=existing.consecutive_failures if existing is not None else 0,
        cooldown_trips=existing.cooldown_trips if existing is not None else 0,
        cold_start_p50_ms=existing.cold_start_p50_ms if existing is not None else None,
        cold_start_p95_ms=existing.cold_start_p95_ms if existing is not None else None,
        recent_error_rate=existing.recent_error_rate if existing is not None else 0.0,
        cooldown_until=existing.cooldown_until if existing is not None else None,
        source=CapabilitySource(str(snapshot["source"])),
        last_applied_yaml_hash=_optional_string(snapshot.get("last_applied_yaml_hash")),
        updated_at=now,
    )


def _require_desired(operation: PlanOperation) -> Snapshot:
    if operation.desired is None:
        raise GitOpsConfigError(
            f"{operation.action.value} {operation.entity_type.value} "
            f"{operation.entity_id} has no desired snapshot"
        )
    return operation.desired


def _has_non_enabled_change(operation: PlanOperation) -> bool:
    return any(field != "enabled" for field in operation.changes)


async def _enable_capability(repo: CapabilityRepositoryLike, capability_id: str) -> None:
    result = await repo.enable(capability_id)
    if result is None:
        raise GitOpsConfigError(f"cannot enable missing capability: {capability_id}")


async def _disable_capability(repo: CapabilityRepositoryLike, capability_id: str) -> None:
    result = await repo.disable(capability_id)
    if result is None:
        raise GitOpsConfigError(f"cannot disable missing capability: {capability_id}")


async def _enable_provider(repo: ProviderRepositoryLike, provider_id: str) -> None:
    result = await repo.enable(provider_id)
    if result is None:
        raise GitOpsConfigError(f"cannot enable missing provider: {provider_id}")


async def _disable_provider(repo: ProviderRepositoryLike, provider_id: str) -> None:
    result = await repo.disable(provider_id)
    if result is None:
        raise GitOpsConfigError(f"cannot disable missing provider: {provider_id}")


def _json_object(value: object, field_name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise GitOpsConfigError(f"{field_name} must be an object")
    return dict(value)


def _list_value(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise GitOpsConfigError("expected a list value in GitOps snapshot")
    return list(value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "AuditWriter",
    "CapabilityRepositoryLike",
    "GitOpsApplyResult",
    "GitOpsDestructiveChangeError",
    "ProviderRepositoryLike",
    "apply_plan",
]
