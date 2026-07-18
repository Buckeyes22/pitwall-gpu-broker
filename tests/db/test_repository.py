"""Repository integration tests for CapabilityRepository, ProviderRepository,
and insert_audit.

Applies every migration in a disposable schema (DROP/recreate per test class),
then exercises each repository method through asyncpg to verify:
  - JSONB config round-trip fidelity (nested objects, arrays, numbers)
  - Transactional audit writes (visible inside tx, rolled back cleanly)
  - Capability CRUD: create, get, get_by_name, list, patch, enable, disable
  - Provider CRUD: create, get, list, patch, enable, disable
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import asyncpg
import pytest

from pitwall.core.enums import (
    CapabilityClass,
    CapabilityHint,
    CapabilitySource,
    CostMode,
    ProviderType,
)
from pitwall.core.models import (
    Capability,
    CapabilityDefaults,
    ConfigAuditEntry,
    Provider,
)
from pitwall.db.repository import (
    CapabilityRepository,
    ProviderRepository,
    insert_audit,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"

_ALL_MIGRATION_SQL = "\n".join(p.read_text() for p in sorted(_MIGRATION_DIR.glob("*.sql")))

_now = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.UTC)


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        pytest.skip("DATABASE_URL is required for repository integration tests")
    return url


async def _make_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        _db_url(),
        min_size=1,
        max_size=4,
        init=_register_json_codec,
    )


async def _register_json_codec(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: json.dumps(v),
        decoder=lambda v: json.loads(v),
        schema="pg_catalog",
    )


def _make_capability(
    cap_id: str = "cap-repo-001",
    name: str = "repo-test-capability",
    version: str = "1.0.0",
    cap_class: CapabilityClass = CapabilityClass.EMBEDDING,
    cost_mode: CostMode = CostMode.PER_REQUEST,
) -> Capability:
    return Capability(
        id=cap_id,
        name=name,
        version=version,
        class_=cap_class,
        cost_mode=cost_mode,
        description="integration test capability",
        input_schema={"type": "object", "properties": {"prompt": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"embedding": {"type": "array"}}},
        defaults=CapabilityDefaults(
            execution_timeout_ms=30_000,
            ttl_ms=120_000,
        ),
        hints_supported=[CapabilityHint.LATENCY_SENSITIVE],
        source=CapabilitySource.API,
        enabled=True,
        created_at=_now,
        updated_at=_now,
    )


def _make_provider(
    prov_id: str = "prov-repo-001",
    capability_id: str = "cap-repo-001",
    name: str = "repo-test-provider",
    provider_type: ProviderType = ProviderType.SERVERLESS_QUEUE,
    priority: int = 1,
    health_status: str = "unknown",
) -> Provider:
    return Provider(
        id=prov_id,
        capability_id=capability_id,
        name=name,
        provider_type=provider_type,
        config={
            "gpu_type_priority": ["NVIDIA A100 80GB"],
            "container_disk_gb": 50,
            "env_vars": {"MODEL_NAME": "bge-m3-v2", "MAX_BATCH_SIZE": "32"},
        },
        priority=priority,
        enabled=True,
        health_status=health_status,
        cold_start_p50_ms=1200,
        cold_start_p95_ms=3400,
        recent_error_rate=0.0,
        source=CapabilitySource.API,
        updated_at=_now,
    )


@pytest.fixture(autouse=True)
async def _ensure_migrations() -> None:
    pool = await _make_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS pitwall CASCADE")
            await conn.execute(_ALL_MIGRATION_SQL)
    finally:
        await pool.close()


class TestCapabilityRepository:
    @pytest.mark.asyncio
    async def test_create_and_get(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            cap = _make_capability()
            created = await repo.create(cap)
            assert created.id == cap.id
            assert created.name == cap.name

            fetched = await repo.get(cap.id)
            assert fetched is not None
            assert fetched.id == cap.id
            assert fetched.description == "integration test capability"
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_create_upserts_on_conflict(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            cap = _make_capability(cap_id="cap-repo-upsert", name="repo-upsert-original")
            await repo.create(cap)
            updated = _make_capability(
                cap_id="cap-repo-upsert",
                name="repo-upsert-updated",
                version="2.0.0",
            )
            result = await repo.create(updated)
            assert result.name == "repo-upsert-updated"
            assert result.version == "2.0.0"
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_create_idempotent_same_id(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            cap = _make_capability(cap_id="cap-repo-idempotent", name="repo-idempotent-cap")
            result1 = await repo.create(cap)
            result2 = await repo.create(cap)
            assert result1.id == result2.id
            assert result1.name == result2.name
            assert result1.version == result2.version
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_create_unique_name_constraint_violation(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            cap1 = _make_capability(cap_id="cap-repo-unique-1", name="repo-unique-cap-name")
            await repo.create(cap1)
            cap2 = _make_capability(cap_id="cap-repo-unique-2", name="repo-unique-cap-name")
            with pytest.raises(asyncpg.UniqueViolationError):
                await repo.create(cap2)
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_get_by_name(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            cap = _make_capability(cap_id="cap-repo-name", name="repo-unique-name-lookup")
            await repo.create(cap)
            fetched = await repo.get_by_name("repo-unique-name-lookup")
            assert fetched is not None
            assert fetched.id == "cap-repo-name"
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            assert await repo.get("nonexistent-cap-id") is None
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_list_all(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            await repo.create(_make_capability())
            caps = await repo.list()
            assert len(caps) >= 1
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_list_enabled_only(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            await repo.create(_make_capability(cap_id="cap-repo-len", name="repo-list-en"))
            enabled = await repo.list(enabled_only=True)
            assert all(c.enabled for c in enabled)
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_list_class_filter(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            await repo.create(_make_capability())
            caps = await repo.list(class_filter="embedding")
            assert all(c.class_ == CapabilityClass.EMBEDDING for c in caps)
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_patch_name(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            await repo.create(_make_capability(cap_id="cap-repo-patch", name="repo-patch-original"))
            patched = await repo.patch("cap-repo-patch", name="repo-patch-updated")
            assert patched is not None
            assert patched.name == "repo-patch-updated"
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_patch_config_jsonb(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            await repo.create(_make_capability(cap_id="cap-repo-pjsonb", name="repo-patch-jsonb"))
            new_config = {
                "description": "patched config",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "array"},
                "defaults": {
                    "execution_timeout_ms": 60000,
                    "ttl_ms": 300000,
                },
                "hints_supported": ["latency_sensitive", "cost_sensitive"],
            }
            patched = await repo.patch("cap-repo-pjsonb", config=new_config)
            assert patched is not None

            fetched = await repo.get("cap-repo-pjsonb")
            assert fetched is not None
            assert fetched.description == "patched config"
            assert len(fetched.hints_supported) == 2
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_enable_disable(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            await repo.create(_make_capability(cap_id="cap-repo-toggle", name="repo-toggle-cap"))
            disabled = await repo.disable("cap-repo-toggle")
            assert disabled is not None
            assert disabled.enabled is False

            enabled = await repo.enable("cap-repo-toggle")
            assert enabled is not None
            assert enabled.enabled is True
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_disable_nonexistent_returns_none(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            assert await repo.disable("nonexistent-toggle") is None
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_enable_nonexistent_returns_none(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            assert await repo.enable("nonexistent-toggle") is None
        finally:
            await pool.close()


class TestProviderRepository:
    @pytest.mark.asyncio
    async def test_create_and_get(self) -> None:
        pool = await _make_pool()
        try:
            cap_repo = CapabilityRepository(pool)
            await cap_repo.create(
                _make_capability(cap_id="cap-repo-pcrud", name="repo-prov-crud-cap")
            )

            prov_repo = ProviderRepository(pool)
            prov = _make_provider(
                prov_id="prov-repo-crud",
                capability_id="cap-repo-pcrud",
                name="repo-prov-crud-name",
            )
            created = await prov_repo.create(prov)
            assert created.id == prov.id
            assert created.name == prov.name

            fetched = await prov_repo.get(prov.id)
            assert fetched is not None
            assert fetched.capability_id == "cap-repo-pcrud"
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_create_upserts_on_conflict(self) -> None:
        pool = await _make_pool()
        try:
            cap_repo = CapabilityRepository(pool)
            await cap_repo.create(
                _make_capability(cap_id="cap-repo-pup", name="repo-prov-upsert-cap")
            )

            prov_repo = ProviderRepository(pool)
            await prov_repo.create(
                _make_provider(
                    prov_id="prov-repo-upsert",
                    capability_id="cap-repo-pup",
                    name="repo-original",
                )
            )
            result = await prov_repo.create(
                _make_provider(
                    prov_id="prov-repo-upsert",
                    capability_id="cap-repo-pup",
                    name="repo-updated",
                )
            )
            assert result.name == "repo-updated"
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self) -> None:
        pool = await _make_pool()
        try:
            prov_repo = ProviderRepository(pool)
            assert await prov_repo.get("nonexistent-prov-id") is None
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_list_all(self) -> None:
        pool = await _make_pool()
        try:
            cap_repo = CapabilityRepository(pool)
            await cap_repo.create(
                _make_capability(cap_id="cap-repo-plst", name="repo-prov-list-cap")
            )
            prov_repo = ProviderRepository(pool)
            await prov_repo.create(
                _make_provider(
                    prov_id="prov-repo-list",
                    capability_id="cap-repo-plst",
                )
            )
            provs = await prov_repo.list()
            assert len(provs) >= 1
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_list_by_capability(self) -> None:
        pool = await _make_pool()
        try:
            cap_repo = CapabilityRepository(pool)
            await cap_repo.create(
                _make_capability(cap_id="cap-repo-pflt", name="repo-prov-filter-cap")
            )

            prov_repo = ProviderRepository(pool)
            await prov_repo.create(
                _make_provider(
                    prov_id="prov-repo-filter",
                    capability_id="cap-repo-pflt",
                )
            )

            filtered = await prov_repo.list(capability_id="cap-repo-pflt")
            assert len(filtered) >= 1
            assert all(p.capability_id == "cap-repo-pflt" for p in filtered)
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_list_enabled_only(self) -> None:
        pool = await _make_pool()
        try:
            cap_repo = CapabilityRepository(pool)
            await cap_repo.create(_make_capability(cap_id="cap-repo-pen", name="repo-prov-en-cap"))
            prov_repo = ProviderRepository(pool)
            await prov_repo.create(
                _make_provider(
                    prov_id="prov-repo-en",
                    capability_id="cap-repo-pen",
                )
            )
            enabled = await prov_repo.list(enabled_only=True)
            assert all(p.enabled for p in enabled)
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_patch_config_jsonb(self) -> None:
        pool = await _make_pool()
        try:
            cap_repo = CapabilityRepository(pool)
            await cap_repo.create(
                _make_capability(cap_id="cap-repo-ppat", name="repo-prov-patch-cap")
            )

            prov_repo = ProviderRepository(pool)
            await prov_repo.create(
                _make_provider(
                    prov_id="prov-repo-patch",
                    capability_id="cap-repo-ppat",
                    name="repo-prov-patch-name",
                )
            )

            new_config = {
                "gpu_type_priority": ["NVIDIA H100 80GB HBM3"],
                "container_disk_gb": 100,
            }
            patched = await prov_repo.patch("prov-repo-patch", config=new_config)
            assert patched is not None
            fetched = await prov_repo.get("prov-repo-patch")
            assert fetched is not None
            assert fetched.config["container_disk_gb"] == 100
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_enable_disable(self) -> None:
        pool = await _make_pool()
        try:
            cap_repo = CapabilityRepository(pool)
            await cap_repo.create(
                _make_capability(cap_id="cap-repo-ptog", name="repo-prov-toggle-cap")
            )

            prov_repo = ProviderRepository(pool)
            await prov_repo.create(
                _make_provider(
                    prov_id="prov-repo-toggle",
                    capability_id="cap-repo-ptog",
                )
            )

            disabled = await prov_repo.disable("prov-repo-toggle")
            assert disabled is not None
            assert disabled.enabled is False

            enabled = await prov_repo.enable("prov-repo-toggle")
            assert enabled is not None
            assert enabled.enabled is True
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_disable_nonexistent_returns_none(self) -> None:
        pool = await _make_pool()
        try:
            prov_repo = ProviderRepository(pool)
            assert await prov_repo.disable("nonexistent-prov-toggle") is None
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_patch_name(self) -> None:
        pool = await _make_pool()
        try:
            cap_repo = CapabilityRepository(pool)
            await cap_repo.create(
                _make_capability(cap_id="cap-repo-pname", name="repo-prov-name-cap")
            )

            prov_repo = ProviderRepository(pool)
            await prov_repo.create(
                _make_provider(
                    prov_id="prov-repo-patch-name",
                    capability_id="cap-repo-pname",
                    name="repo-prov-original-name",
                )
            )
            patched = await prov_repo.patch("prov-repo-patch-name", name="repo-prov-updated-name")
            assert patched is not None
            assert patched.name == "repo-prov-updated-name"
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_patch_priority(self) -> None:
        pool = await _make_pool()
        try:
            cap_repo = CapabilityRepository(pool)
            await cap_repo.create(
                _make_capability(cap_id="cap-repo-pprio", name="repo-prov-prio-cap")
            )

            prov_repo = ProviderRepository(pool)
            await prov_repo.create(
                _make_provider(
                    prov_id="prov-repo-patch-prio",
                    capability_id="cap-repo-pprio",
                    priority=10,
                )
            )
            patched = await prov_repo.patch("prov-repo-patch-prio", priority=5)
            assert patched is not None
            assert patched.priority == 5
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_patch_health_status(self) -> None:
        pool = await _make_pool()
        try:
            cap_repo = CapabilityRepository(pool)
            await cap_repo.create(
                _make_capability(cap_id="cap-repo-phlth", name="repo-prov-hlth-cap")
            )

            prov_repo = ProviderRepository(pool)
            await prov_repo.create(
                _make_provider(
                    prov_id="prov-repo-patch-hlth",
                    capability_id="cap-repo-phlth",
                    health_status="unknown",
                )
            )
            patched = await prov_repo.patch("prov-repo-patch-hlth", health_status="healthy")
            assert patched is not None
            assert patched.health_status == "healthy"
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_create_idempotent_same_id(self) -> None:
        pool = await _make_pool()
        try:
            cap_repo = CapabilityRepository(pool)
            await cap_repo.create(
                _make_capability(cap_id="cap-repo-pidem", name="repo-prov-idem-cap")
            )

            prov_repo = ProviderRepository(pool)
            prov = _make_provider(
                prov_id="prov-repo-idempotent",
                capability_id="cap-repo-pidem",
                name="repo-prov-idempotent",
            )
            result1 = await prov_repo.create(prov)
            result2 = await prov_repo.create(prov)
            assert result1.id == result2.id
            assert result1.name == result2.name
        finally:
            await pool.close()


class TestJsonbConfigRoundTrip:
    @pytest.mark.asyncio
    async def test_capability_jsonb_nested_round_trip(self) -> None:
        pool = await _make_pool()
        try:
            repo = CapabilityRepository(pool)
            cap = Capability(
                id="cap-repo-jsonb-deep",
                name="repo-jsonb-deep-cap",
                version="1.0.0",
                class_=CapabilityClass.LLM,
                cost_mode=CostMode.PER_TOKEN,
                description="deeply nested test",
                input_schema={
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "maxLength": 8192,
                            "metadata": {
                                "pii": False,
                                "multiline": True,
                            },
                        },
                        "temperature": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 2,
                        },
                    },
                    "required": ["prompt"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "embedding": {
                            "type": "array",
                            "items": {"type": "number"},
                        },
                        "model_version": {"type": "string"},
                    },
                },
                defaults=CapabilityDefaults(
                    execution_timeout_ms=120000,
                    ttl_ms=600000,
                ),
                hints_supported=[
                    CapabilityHint.LATENCY_SENSITIVE,
                    CapabilityHint.COST_SENSITIVE,
                    CapabilityHint.REGION_PREFERENCE,
                ],
                source=CapabilitySource.API,
                created_at=_now,
                updated_at=_now,
            )
            await repo.create(cap)
            fetched = await repo.get("cap-repo-jsonb-deep")
            assert fetched is not None

            assert fetched.input_schema["properties"]["prompt"]["maxLength"] == 8192
            assert fetched.input_schema["properties"]["prompt"]["metadata"]["pii"] is False
            assert fetched.output_schema["properties"]["embedding"]["items"]["type"] == "number"
            assert len(fetched.hints_supported) == 3
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_provider_jsonb_config_round_trip(self) -> None:
        pool = await _make_pool()
        try:
            cap_repo = CapabilityRepository(pool)
            await cap_repo.create(
                _make_capability(cap_id="cap-repo-pjsonb", name="repo-prov-jsonb-cap")
            )

            prov_repo = ProviderRepository(pool)
            prov = Provider(
                id="prov-repo-jsonb-complex",
                capability_id="cap-repo-pjsonb",
                name="repo-jsonb-complex-provider",
                provider_type=ProviderType.SERVERLESS_LB,
                config={
                    "gpu_type_priority": [
                        "NVIDIA A100 80GB",
                        "NVIDIA H100 80GB HBM3",
                    ],
                    "container_disk_gb": 100,
                    "env_vars": {
                        "MODEL_NAME": "bge-m3-v2",
                        "MAX_BATCH_SIZE": "32",
                        "NESTED": '{"key": "value"}',
                    },
                    "volume_mount_path": "/data/models",
                },
                priority=5,
                source=CapabilitySource.API,
                updated_at=_now,
            )
            await prov_repo.create(prov)
            fetched = await prov_repo.get("prov-repo-jsonb-complex")
            assert fetched is not None
            assert fetched.config["gpu_type_priority"] == [
                "NVIDIA A100 80GB",
                "NVIDIA H100 80GB HBM3",
            ]
            assert fetched.config["env_vars"]["MODEL_NAME"] == "bge-m3-v2"
            assert fetched.config["volume_mount_path"] == "/data/models"
        finally:
            await pool.close()


class TestTransactionalAuditWrites:
    @pytest.mark.asyncio
    async def test_insert_audit_round_trip(self) -> None:
        pool = await _make_pool()
        try:
            entry = await insert_audit(
                pool,
                actor="rest:admin",
                action="update",
                entity_type="capability",
                entity_id="cap-repo-audit-rt",
                old_value={
                    "cost_mode": "per_request",
                    "version": "1.0.0",
                },
                new_value={
                    "cost_mode": "per_token",
                    "version": "2.0.0",
                },
                change_reason="version bump",
            )
            assert isinstance(entry, ConfigAuditEntry)
            assert entry.id is not None
            assert entry.actor == "rest:admin"
            assert entry.action == "update"
            assert entry.entity_type == "capability"
            assert entry.entity_id == "cap-repo-audit-rt"
            assert entry.old_value is not None
            assert entry.old_value["cost_mode"] == "per_request"
            assert entry.new_value is not None
            assert entry.new_value["cost_mode"] == "per_token"
            assert entry.change_reason == "version bump"
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_audit_null_old_value(self) -> None:
        pool = await _make_pool()
        try:
            entry = await insert_audit(
                pool,
                actor="system",
                action="create",
                entity_type="capability",
                entity_id="cap-repo-audit-null",
                new_value={"name": "test"},
            )
            assert entry.old_value is None
            assert entry.new_value is not None
            assert entry.new_value["name"] == "test"
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_audit_visible_in_transaction(self) -> None:
        pool = await _make_pool()
        try:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    """
                        INSERT INTO pitwall.config_audit
                            (actor, action, entity_type, entity_id, new_value)
                        VALUES ($1, $2, $3, $4, $5::jsonb)
                        """,
                    "system",
                    "create",
                    "capability",
                    "cap-repo-tx-visible",
                    {"name": "tx-test"},
                )
                row = await conn.fetchrow(
                    "SELECT * FROM pitwall.config_audit "
                    "WHERE entity_id = 'cap-repo-tx-visible' "
                    "AND action = 'create'"
                )
                assert row is not None
                assert row["actor"] == "system"
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_audit_rollback_clean(self) -> None:
        pool = await _make_pool()
        try:
            async with pool.acquire() as conn:
                tx = conn.transaction()
                await tx.start()
                await conn.execute(
                    """
                    INSERT INTO pitwall.config_audit
                        (actor, action, entity_type, entity_id, new_value)
                    VALUES ($1, $2, $3, $4, $5::jsonb)
                    """,
                    "system",
                    "create",
                    "capability",
                    "cap-repo-tx-rollback",
                    {"name": "rollback-test"},
                )
                await tx.rollback()

            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM pitwall.config_audit WHERE entity_id = 'cap-repo-tx-rollback'"
                )
                assert row is None
        finally:
            await pool.close()

    @pytest.mark.asyncio
    async def test_audit_created_at_defaults_to_now(self) -> None:
        pool = await _make_pool()
        try:
            entry = await insert_audit(
                pool,
                actor="system",
                action="create",
                entity_type="provider",
                entity_id="prov-repo-audit-ts",
                new_value={"enabled": True},
            )
            assert entry.created_at is not None
            delta = abs((dt.datetime.now(dt.UTC) - entry.created_at).total_seconds())
            assert delta < 10
        finally:
            await pool.close()
