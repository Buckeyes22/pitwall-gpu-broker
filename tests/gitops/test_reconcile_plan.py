from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability, CapabilityDefaults, Provider
from pitwall.gitops import (
    GitOpsConfigError,
    GitOpsDestructiveChangeError,
    PlanAction,
    PlanEntityType,
    PlanOperation,
    ReconcilePlan,
    apply_plan,
    build_reconcile_plan,
    load_desired_state,
)

_NOW = dt.datetime(2026, 6, 2, 12, 0, 0, tzinfo=dt.UTC)


def _capability(
    *,
    id: str = "cap_embedding_demo",
    name: str = "embedding.demo",
    version: str = "1.0.0",
    class_: CapabilityClass = CapabilityClass.EMBEDDING,
    description: str | None = "Demo embedding capability",
    cost_mode: CostMode = CostMode.PER_SECOND,
    enabled: bool = True,
    source: CapabilitySource = CapabilitySource.YAML,
    yaml_hash: str | None = "hash-old",
) -> Capability:
    return Capability(
        id=id,
        name=name,
        version=version,
        class_=class_,
        description=description,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        defaults=CapabilityDefaults(),
        cost_mode=cost_mode,
        hints_supported=[],
        source=source,
        last_applied_yaml_hash=yaml_hash,
        enabled=enabled,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider(
    *,
    id: str = "prov_embedding_demo",
    capability_id: str = "cap_embedding_demo",
    name: str = "embedding-demo-lb",
    provider_type: ProviderType = ProviderType.SERVERLESS_LB,
    priority: int = 10,
    enabled: bool = True,
    source: CapabilitySource = CapabilitySource.YAML,
    yaml_hash: str | None = "hash-old",
) -> Provider:
    return Provider(
        id=id,
        capability_id=capability_id,
        name=name,
        provider_type=provider_type,
        runpod_endpoint_id="eptest00000000",
        runpod_template_id=None,
        region="US-KS-2",
        cloud_type=None,
        config={"lb_base_url": "https://eptest00000000.api.runpod.ai"},
        priority=priority,
        enabled=enabled,
        health_status="healthy",
        consecutive_failures=3,
        cooldown_trips=1,
        cold_start_p50_ms=8000,
        cold_start_p95_ms=22000,
        recent_error_rate=0.25,
        cooldown_until=None,
        source=source,
        last_applied_yaml_hash=yaml_hash,
        updated_at=_NOW,
    )


def _desired_yaml(*, priority: int = 10, enabled: bool = True) -> str:
    return f"""
apiVersion: pitwall.dev/v1
capabilities:
  - name: embedding.demo
    class: embedding
    cost_mode: per_second
    description: Demo embedding capability
    input_schema: {{"type": "object"}}
    output_schema: {{"type": "object"}}
providers:
  - name: embedding-demo-lb
    capability: embedding.demo
    provider_type: serverless_lb
    runpod_endpoint_id: eptest00000000
    region: US-KS-2
    priority: {priority}
    enabled: {str(enabled).lower()}
    config:
      lb_base_url: https://eptest00000000.api.runpod.ai
"""


def _write_desired(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pitwall-gitops.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class _CapabilityRepo:
    def __init__(self, current: Sequence[Capability] = ()) -> None:
        self._items = {item.id: item for item in current}
        self.created: list[Capability] = []
        self.enabled: list[str] = []
        self.disabled: list[str] = []

    async def get(self, capability_id: str) -> Capability | None:
        return self._items.get(capability_id)

    async def create(self, capability: Capability) -> Capability:
        self.created.append(capability)
        self._items[capability.id] = capability
        return capability

    async def enable(self, capability_id: str) -> Capability | None:
        self.enabled.append(capability_id)
        item = self._items.get(capability_id)
        if item is None:
            return None
        self._items[capability_id] = item.model_copy(update={"enabled": True})
        return self._items[capability_id]

    async def disable(self, capability_id: str) -> Capability | None:
        self.disabled.append(capability_id)
        item = self._items.get(capability_id)
        if item is None:
            return None
        self._items[capability_id] = item.model_copy(update={"enabled": False})
        return self._items[capability_id]


class _ProviderRepo:
    def __init__(self, current: Sequence[Provider] = ()) -> None:
        self._items = {item.id: item for item in current}
        self.created: list[Provider] = []
        self.enabled: list[str] = []
        self.disabled: list[str] = []

    async def get(self, provider_id: str) -> Provider | None:
        return self._items.get(provider_id)

    async def create(self, provider: Provider) -> Provider:
        self.created.append(provider)
        self._items[provider.id] = provider
        return provider

    async def enable(self, provider_id: str) -> Provider | None:
        self.enabled.append(provider_id)
        item = self._items.get(provider_id)
        if item is None:
            return None
        self._items[provider_id] = item.model_copy(update={"enabled": True})
        return self._items[provider_id]

    async def disable(self, provider_id: str) -> Provider | None:
        self.disabled.append(provider_id)
        item = self._items.get(provider_id)
        if item is None:
            return None
        self._items[provider_id] = item.model_copy(update={"enabled": False})
        return self._items[provider_id]


class _AuditRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        pool: object,
        *,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        old_value: dict[str, Any] | None = None,
        new_value: dict[str, Any] | None = None,
        change_reason: str | None = None,
    ) -> object:
        self.calls.append(
            {
                "pool": pool,
                "actor": actor,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "old_value": old_value,
                "new_value": new_value,
                "change_reason": change_reason,
            }
        )
        return object()


def test_load_desired_state_requires_versioned_yaml_root(tmp_path: Path) -> None:
    path = _write_desired(
        tmp_path,
        """
capabilities:
  - name: embedding.demo
    class: embedding
""",
    )

    with pytest.raises(GitOpsConfigError, match="apiVersion"):
        load_desired_state([path])


def test_build_plan_creates_capability_then_provider_from_yaml(tmp_path: Path) -> None:
    state = load_desired_state([_write_desired(tmp_path, _desired_yaml())])
    plan = build_reconcile_plan(state, current_capabilities=[], current_providers=[])

    assert [(op.entity_type, op.action, op.entity_id) for op in plan.operations] == [
        (PlanEntityType.CAPABILITY, PlanAction.CREATE, "cap_embedding_demo"),
        (PlanEntityType.PROVIDER, PlanAction.CREATE, "prov_embedding_demo_lb"),
    ]
    assert plan.counts == {"create": 2, "update": 0, "delete": 0}
    assert plan.has_destructive_changes is False


def test_build_plan_is_empty_when_current_matches_desired(tmp_path: Path) -> None:
    state = load_desired_state([_write_desired(tmp_path, _desired_yaml())])
    yaml_hash = state.capabilities[0].yaml_hash
    plan = build_reconcile_plan(
        state,
        current_capabilities=[_capability(yaml_hash=yaml_hash)],
        current_providers=[_provider(yaml_hash=yaml_hash)],
    )

    assert plan.operations == ()
    assert plan.counts == {"create": 0, "update": 0, "delete": 0}


def test_build_plan_reports_capability_field_updates(tmp_path: Path) -> None:
    state = load_desired_state(
        [
            _write_desired(
                tmp_path,
                _desired_yaml().replace(
                    "description: Demo embedding capability",
                    "description: Updated description",
                ),
            )
        ]
    )
    plan = build_reconcile_plan(
        state,
        current_capabilities=[_capability(description="Old description")],
        current_providers=[_provider(yaml_hash=state.providers[0].yaml_hash)],
    )

    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.entity_type == PlanEntityType.CAPABILITY
    assert operation.action == PlanAction.UPDATE
    assert operation.changes["description"].current == "Old description"
    assert operation.changes["description"].desired == "Updated description"


def test_build_plan_reports_provider_updates(tmp_path: Path) -> None:
    state = load_desired_state([_write_desired(tmp_path, _desired_yaml(priority=3))])
    plan = build_reconcile_plan(
        state,
        current_capabilities=[_capability(yaml_hash=state.capabilities[0].yaml_hash)],
        current_providers=[_provider(priority=10)],
    )

    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.entity_type == PlanEntityType.PROVIDER
    assert operation.action == PlanAction.UPDATE
    assert operation.changes["priority"].current == 10
    assert operation.changes["priority"].desired == 3


def test_build_plan_deletes_only_yaml_owned_absent_resources(tmp_path: Path) -> None:
    state = load_desired_state([_write_desired(tmp_path, _desired_yaml())])
    old_yaml_capability = _capability(
        id="cap_old_yaml",
        name="embedding.old",
        source=CapabilitySource.YAML,
    )
    old_api_capability = _capability(
        id="cap_old_api",
        name="embedding.api",
        source=CapabilitySource.API,
        yaml_hash=None,
    )
    old_yaml_provider = _provider(
        id="prov_old_yaml",
        name="old-yaml-provider",
        capability_id="cap_old_yaml",
        source=CapabilitySource.YAML,
    )
    old_api_provider = _provider(
        id="prov_old_api",
        name="old-api-provider",
        capability_id="cap_old_api",
        source=CapabilitySource.API,
        yaml_hash=None,
    )

    plan = build_reconcile_plan(
        state,
        current_capabilities=[
            _capability(yaml_hash=state.capabilities[0].yaml_hash),
            old_yaml_capability,
            old_api_capability,
        ],
        current_providers=[
            _provider(yaml_hash=state.providers[0].yaml_hash),
            old_yaml_provider,
            old_api_provider,
        ],
    )

    assert [(op.entity_type, op.action, op.entity_id) for op in plan.operations] == [
        (PlanEntityType.PROVIDER, PlanAction.DELETE, "prov_old_yaml"),
        (PlanEntityType.CAPABILITY, PlanAction.DELETE, "cap_old_yaml"),
    ]
    assert plan.has_destructive_changes is True


@pytest.mark.anyio
async def test_apply_plan_defaults_to_dry_run_without_mutating_or_auditing(
    tmp_path: Path,
) -> None:
    state = load_desired_state([_write_desired(tmp_path, _desired_yaml())])
    plan = build_reconcile_plan(state, current_capabilities=[], current_providers=[])
    capability_repo = _CapabilityRepo()
    provider_repo = _ProviderRepo()
    audit = _AuditRecorder()

    result = await apply_plan(
        plan,
        capability_repo=capability_repo,
        provider_repo=provider_repo,
        pool=object(),
        audit_writer=audit,
    )

    assert result.dry_run is True
    assert result.applied is False
    assert capability_repo.created == []
    assert provider_repo.created == []
    assert audit.calls == []


@pytest.mark.anyio
async def test_apply_plan_requires_delete_flag_for_destructive_operations(
    tmp_path: Path,
) -> None:
    state = load_desired_state([_write_desired(tmp_path, _desired_yaml())])
    plan = build_reconcile_plan(
        state,
        current_capabilities=[
            _capability(yaml_hash=state.capabilities[0].yaml_hash),
            _capability(id="cap_old_yaml", name="embedding.old"),
        ],
        current_providers=[_provider(yaml_hash=state.providers[0].yaml_hash)],
    )

    with pytest.raises(GitOpsDestructiveChangeError, match="allow_delete"):
        await apply_plan(
            plan,
            capability_repo=_CapabilityRepo(),
            provider_repo=_ProviderRepo(),
            pool=object(),
            dry_run=False,
        )


@pytest.mark.anyio
async def test_apply_plan_treats_direct_delete_operation_as_destructive() -> None:
    plan = ReconcilePlan(
        operations=(
            PlanOperation(
                action=PlanAction.DELETE,
                entity_type=PlanEntityType.PROVIDER,
                entity_id="prov_manual_delete",
                name="manual-delete-provider",
            ),
        )
    )

    assert plan.has_destructive_changes is True
    with pytest.raises(GitOpsDestructiveChangeError, match="allow_delete"):
        await apply_plan(
            plan,
            capability_repo=_CapabilityRepo(),
            provider_repo=_ProviderRepo(),
            pool=object(),
            dry_run=False,
        )


@pytest.mark.anyio
async def test_apply_plan_creates_updates_disables_and_audits_in_order(
    tmp_path: Path,
) -> None:
    state = load_desired_state([_write_desired(tmp_path, _desired_yaml(enabled=False))])
    current_capability = _capability(
        description="old",
        yaml_hash="old-hash",
        enabled=True,
    )
    current_provider = _provider(priority=99, yaml_hash="old-hash", enabled=True)
    old_provider = _provider(
        id="prov_old_yaml",
        name="old-yaml-provider",
        capability_id="cap_embedding_demo",
    )
    plan = build_reconcile_plan(
        state,
        current_capabilities=[current_capability],
        current_providers=[current_provider, old_provider],
    )
    capability_repo = _CapabilityRepo([current_capability])
    provider_repo = _ProviderRepo([current_provider, old_provider])
    audit = _AuditRecorder()

    result = await apply_plan(
        plan,
        capability_repo=capability_repo,
        provider_repo=provider_repo,
        pool="pool",
        dry_run=False,
        allow_delete=True,
        actor="gitops:test",
        change_reason="unit-test",
        audit_writer=audit,
    )

    assert result.applied is True
    assert [provider.id for provider in provider_repo.created] == ["prov_embedding_demo"]
    assert provider_repo.created[0].health_status == "healthy"
    assert provider_repo.created[0].consecutive_failures == 3
    assert provider_repo.disabled == ["prov_embedding_demo", "prov_old_yaml"]
    assert [cap.id for cap in capability_repo.created] == ["cap_embedding_demo"]
    assert capability_repo.disabled == []
    assert [(call["action"], call["entity_type"], call["entity_id"]) for call in audit.calls] == [
        ("gitops:update", "capability", "cap_embedding_demo"),
        ("gitops:update", "provider", "prov_embedding_demo"),
        ("gitops:delete", "provider", "prov_old_yaml"),
    ]
    assert all(call["actor"] == "gitops:test" for call in audit.calls)
    assert all(call["change_reason"] == "unit-test" for call in audit.calls)
