"""Async repository interfaces for Pitwall registry tables.

Exposes async methods for create, get, list, patch, enable, disable, and
audit insert on the ``capabilities`` and ``providers`` tables.  Every method
returns a pydantic model instance, never a raw asyncpg row.
"""

from __future__ import annotations

import builtins
import datetime as dt
from dataclasses import dataclass
from typing import Any

import asyncpg

from pitwall.core.enums import CapabilitySource, WorkloadState
from pitwall.core.models import (
    Capability,
    ConfigAuditEntry,
    JsonObject,
    Lease,
    LeaseEndpoints,
    LeaseReadiness,
    Provider,
    WebhookDeliveryFailure,
    WebhookSubscription,
    Workload,
)
from pitwall.webhook_dispatcher.secret_store import (
    EncryptedWebhookSecret,
    WebhookSecretCipher,
)

_UNSET = object()
LEASE_MUTATION_UNSET = _UNSET
_CLEARABLE_PROVIDER_FIELDS = {"cooldown_until", "last_applied_yaml_hash"}
_WORKLOAD_JSONB_COLUMNS = frozenset({"input", "result", "error"})


@dataclass(frozen=True, slots=True)
class LeaseMutationResult:
    """Result of an atomic lease mutation."""

    lease: Lease
    replayed: bool = False


class LeaseMutationStateError(RuntimeError):
    """The lease changed to a state in which the mutation is not valid."""

    def __init__(self, state: str, operation: str) -> None:
        super().__init__(f"{state}:{operation}")
        self.state = state
        self.operation = operation


class LeaseMutationExpiryLimitError(RuntimeError):
    """The requested renewal would exceed the configured expiry horizon."""


class LeaseMutationIdempotencyError(RuntimeError):
    """An idempotency key was reused for a different lease mutation."""

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(idempotency_key)
        self.idempotency_key = idempotency_key


def _capability_from_row(row: asyncpg.Record) -> Capability:
    config = row["config"] if isinstance(row["config"], dict) else {}
    return Capability(
        id=row["id"],
        name=row["name"],
        version=row["version"],
        class_=row["class"],
        description=config.get("description"),
        input_schema=config.get("input_schema", {}),
        output_schema=config.get("output_schema", {}),
        defaults=config.get("defaults", {}),
        cost_mode=row["cost_mode"],
        hints_supported=config.get("hints_supported", []),
        source=row.get("source", CapabilitySource.API),
        last_applied_yaml_hash=row.get("last_applied_yaml_hash"),
        enabled=row.get("enabled", True),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _capability_config_payload(cap: Capability) -> dict[str, Any]:
    return {
        "description": cap.description,
        "input_schema": cap.input_schema,
        "output_schema": cap.output_schema,
        "defaults": cap.defaults.model_dump(),
        "hints_supported": [h.value for h in cap.hints_supported],
    }


def _provider_from_row(row: asyncpg.Record) -> Provider:
    return Provider(
        id=row["id"],
        capability_id=row["capability_id"],
        name=row["name"],
        provider_type=row["provider_type"],
        runpod_endpoint_id=row.get("runpod_endpoint_id"),
        runpod_template_id=row.get("runpod_template_id"),
        region=row.get("region"),
        cloud_type=row.get("cloud_type"),
        config=row["config"] if isinstance(row["config"], dict) else {},
        priority=row["priority"],
        enabled=row.get("enabled", True),
        health_status=row.get("health_status", "unknown"),
        consecutive_failures=row.get("consecutive_failures", 0),
        cooldown_trips=row.get("cooldown_trips", 0),
        cold_start_p50_ms=row.get("cold_start_p50_ms"),
        cold_start_p95_ms=row.get("cold_start_p95_ms"),
        recent_error_rate=row.get("recent_error_rate", 0),
        cooldown_until=row.get("cooldown_until"),
        source=row.get("source", CapabilitySource.API),
        last_applied_yaml_hash=row.get("last_applied_yaml_hash"),
        updated_at=row["updated_at"],
    )


def _audit_from_row(row: asyncpg.Record) -> ConfigAuditEntry:
    old = row.get("old_value")
    new = row.get("new_value")
    return ConfigAuditEntry(
        id=row["id"],
        actor=row["actor"],
        action=row["action"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        old_value=old if isinstance(old, dict) else None,
        new_value=new if isinstance(new, dict) else None,
        change_reason=row.get("change_reason"),
        created_at=row["created_at"],
    )


def _endpoints_from_row(row: asyncpg.Record) -> LeaseEndpoints | None:
    raw = row.get("endpoints")
    if raw is None:
        return None
    if isinstance(raw, dict):
        return LeaseEndpoints.model_validate(raw)
    return None


def _readiness_from_row(row: asyncpg.Record) -> LeaseReadiness | None:
    raw = row.get("readiness")
    if raw is None:
        return None
    if isinstance(raw, dict):
        return LeaseReadiness.model_validate(raw)
    return None


def _lease_from_row(row: asyncpg.Record) -> Lease:
    return Lease(
        id=row["id"],
        provider_id=row["provider_id"],
        runpod_pod_id=row["runpod_pod_id"],
        state=row["state"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        renewal_policy=row["renewal_policy"],
        auto_teardown_on_expiry=row.get("auto_teardown_on_expiry", True),
        endpoints=_endpoints_from_row(row),
        readiness=_readiness_from_row(row),
        cost_accrued_usd=row.get("cost_accrued_usd"),
        last_health_at=row.get("last_health_at"),
        terminated_at=row.get("terminated_at"),
        terminated_reason=row.get("terminated_reason"),
    )


class CapabilityRepository:
    """Async repository for ``pitwall.capabilities``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(self, cap: Capability) -> Capability:
        config = _capability_config_payload(cap)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO pitwall.capabilities
                    (id, name, version, class, cost_mode, config, source,
                     last_applied_yaml_hash, enabled, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11)
                ON CONFLICT (id) DO UPDATE SET
                    name       = EXCLUDED.name,
                    version    = EXCLUDED.version,
                    class      = EXCLUDED.class,
                    cost_mode  = EXCLUDED.cost_mode,
                    config     = EXCLUDED.config,
                    source     = EXCLUDED.source,
                    last_applied_yaml_hash = EXCLUDED.last_applied_yaml_hash,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                cap.id,
                cap.name,
                cap.version,
                cap.class_.value,
                cap.cost_mode.value,
                config,
                cap.source.value,
                cap.last_applied_yaml_hash,
                True,
                cap.created_at,
                cap.updated_at,
            )
        assert row is not None
        return _capability_from_row(row)

    async def get(self, capability_id: str) -> Capability | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM pitwall.capabilities WHERE id = $1",
                capability_id,
            )
        if row is None:
            return None
        return _capability_from_row(row)

    async def get_by_name(self, name: str) -> Capability | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM pitwall.capabilities WHERE name = $1",
                name,
            )
        if row is None:
            return None
        return _capability_from_row(row)

    async def list(
        self,
        *,
        enabled_only: bool = False,
        class_filter: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Capability]:
        conditions: list[str] = []
        params: list[Any] = []
        idx = 1

        if enabled_only:
            conditions.append("enabled = true")
        if class_filter is not None:
            conditions.append(f"class = ${idx}")
            params.append(class_filter)
            idx += 1

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = (
            f"SELECT * FROM pitwall.capabilities{where}"
            f" ORDER BY name LIMIT ${idx} OFFSET ${idx + 1}"
        )
        params.extend([limit, offset])

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [_capability_from_row(r) for r in rows]

    async def patch(
        self,
        capability_id: str,
        *,
        name: str | None = None,
        version: str | None = None,
        class_: str | None = None,
        cost_mode: str | None = None,
        config: JsonObject | None = None,
        source: str | None = None,
        last_applied_yaml_hash: str | None | object = _UNSET,
    ) -> Capability | None:
        sets: list[str] = ["updated_at = now()"]
        params: list[Any] = []
        idx = 1

        if name is not None:
            sets.append(f"name = ${idx}")
            params.append(name)
            idx += 1
        if version is not None:
            sets.append(f"version = ${idx}")
            params.append(version)
            idx += 1
        if class_ is not None:
            sets.append(f"class = ${idx}")
            params.append(class_)
            idx += 1
        if cost_mode is not None:
            sets.append(f"cost_mode = ${idx}")
            params.append(cost_mode)
            idx += 1
        if config is not None:
            sets.append(f"config = ${idx}::jsonb")
            params.append(config)
            idx += 1
        if source is not None:
            sets.append(f"source = ${idx}")
            params.append(source)
            idx += 1
        if last_applied_yaml_hash is not _UNSET:
            sets.append(f"last_applied_yaml_hash = ${idx}")
            params.append(last_applied_yaml_hash)
            idx += 1

        params.append(capability_id)
        query = f"UPDATE pitwall.capabilities SET {', '.join(sets)} WHERE id = ${idx} RETURNING *"

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        if row is None:
            return None
        return _capability_from_row(row)

    async def enable(self, capability_id: str) -> Capability | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE pitwall.capabilities SET enabled = true, updated_at = now()"
                " WHERE id = $1 RETURNING *",
                capability_id,
            )
        if row is None:
            return None
        return _capability_from_row(row)

    async def disable(self, capability_id: str) -> Capability | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE pitwall.capabilities SET enabled = false, updated_at = now()"
                " WHERE id = $1 RETURNING *",
                capability_id,
            )
        if row is None:
            return None
        return _capability_from_row(row)

    async def upsert(
        self,
        name: str,
        class_: str,
        cost_mode: str,
        *,
        version: str = "1.0.0",
        description: str | None = None,
        input_schema: JsonObject | None = None,
        output_schema: JsonObject | None = None,
        hints_supported: builtins.list[str] | None = None,
        openai_compatible: bool = False,
        enabled: bool = True,
    ) -> Capability:
        """Upsert a capability by name, ensuring it exists with the specified properties.

        If the capability does not exist, it is created with the given properties.
        If the capability exists, it is updated to match the given properties.

        Args:
            name: Capability name (e.g., "llm.qwen3-32b")
            class_: Capability class (e.g., "llm", "embedding")
            cost_mode: Cost mode (e.g., "per_second", "per_request", "per_token")
            version: Capability version string
            description: Human-readable description
            input_schema: JSON schema for inputs
            output_schema: JSON schema for outputs
            hints_supported: List of capability hints
            openai_compatible: Whether this capability is OpenAI-compatible
            enabled: Whether the capability is enabled

        Returns:
            The upserted Capability
        """
        now = dt.datetime.now(dt.UTC)
        cap_id = f"cap_{name.replace('.', '_').replace('-', '_')}"
        config: dict[str, Any] = {
            "description": description,
            "input_schema": input_schema or {},
            "output_schema": output_schema or {},
            "defaults": {
                "execution_timeout_ms": 60_000,
                "ttl_ms": 300_000,
                "result_delivery": "sync",
            },
            "hints_supported": hints_supported or [],
            "openai_compatible": openai_compatible,
        }
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO pitwall.capabilities
                    (id, name, version, class, cost_mode, config, source,
                     last_applied_yaml_hash, enabled, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'api', NULL, $7, $8, $8)
                ON CONFLICT (name) DO UPDATE SET
                    version    = EXCLUDED.version,
                    class      = EXCLUDED.class,
                    cost_mode  = EXCLUDED.cost_mode,
                    config     = EXCLUDED.config,
                    source     = EXCLUDED.source,
                    last_applied_yaml_hash = EXCLUDED.last_applied_yaml_hash,
                    enabled    = EXCLUDED.enabled,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                cap_id,
                name,
                version,
                class_,
                cost_mode,
                config,
                enabled,
                now,
            )
        assert row is not None
        return _capability_from_row(row)


class ProviderRepository:
    """Async repository for ``pitwall.providers``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(self, provider: Provider) -> Provider:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO pitwall.providers
                    (id, capability_id, name, provider_type, runpod_endpoint_id,
                     runpod_template_id, region, cloud_type, config, priority,
                     enabled, health_status, consecutive_failures, cooldown_trips,
                     cold_start_p50_ms, cold_start_p95_ms, recent_error_rate,
                     cooldown_until, source,
                     last_applied_yaml_hash, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12,
                        $13, $14, $15, $16, $17, $18, $19, $20, $21)
                ON CONFLICT (id) DO UPDATE SET
                    name           = EXCLUDED.name,
                    provider_type  = EXCLUDED.provider_type,
                    runpod_endpoint_id = EXCLUDED.runpod_endpoint_id,
                    runpod_template_id = EXCLUDED.runpod_template_id,
                    region         = EXCLUDED.region,
                    cloud_type     = EXCLUDED.cloud_type,
                    config         = EXCLUDED.config,
                    priority       = EXCLUDED.priority,
                    health_status  = EXCLUDED.health_status,
                    consecutive_failures = EXCLUDED.consecutive_failures,
                    cooldown_trips = EXCLUDED.cooldown_trips,
                    cold_start_p50_ms = EXCLUDED.cold_start_p50_ms,
                    cold_start_p95_ms = EXCLUDED.cold_start_p95_ms,
                    recent_error_rate = EXCLUDED.recent_error_rate,
                    cooldown_until = EXCLUDED.cooldown_until,
                    source         = EXCLUDED.source,
                    last_applied_yaml_hash = EXCLUDED.last_applied_yaml_hash,
                    updated_at     = EXCLUDED.updated_at
                RETURNING *
                """,
                provider.id,
                provider.capability_id,
                provider.name,
                provider.provider_type.value,
                provider.runpod_endpoint_id,
                provider.runpod_template_id,
                provider.region,
                provider.cloud_type,
                provider.config,
                provider.priority,
                provider.enabled,
                provider.health_status,
                provider.consecutive_failures,
                provider.cooldown_trips,
                provider.cold_start_p50_ms,
                provider.cold_start_p95_ms,
                provider.recent_error_rate,
                provider.cooldown_until,
                provider.source.value,
                provider.last_applied_yaml_hash,
                provider.updated_at,
            )
        assert row is not None
        return _provider_from_row(row)

    async def get(self, provider_id: str) -> Provider | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM pitwall.providers WHERE id = $1",
                provider_id,
            )
        if row is None:
            return None
        return _provider_from_row(row)

    async def get_by_name(self, name: str) -> Provider | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM pitwall.providers WHERE name = $1",
                name,
            )
        if row is None:
            return None
        return _provider_from_row(row)

    async def list(
        self,
        *,
        capability_id: str | None = None,
        enabled_only: bool = False,
        provider_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Provider]:
        conditions: list[str] = []
        params: list[Any] = []
        idx = 1

        if capability_id is not None:
            conditions.append(f"capability_id = ${idx}")
            params.append(capability_id)
            idx += 1
        if enabled_only:
            conditions.append("enabled = true")
        if provider_type is not None:
            conditions.append(f"provider_type = ${idx}")
            params.append(provider_type)
            idx += 1

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = (
            f"SELECT * FROM pitwall.providers{where}"
            f" ORDER BY priority, name LIMIT ${idx} OFFSET ${idx + 1}"
        )
        params.extend([limit, offset])

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [_provider_from_row(r) for r in rows]

    async def patch(
        self,
        provider_id: str,
        *,
        name: str | None = None,
        provider_type: str | None = None,
        runpod_endpoint_id: str | None | object = _UNSET,
        runpod_template_id: str | None | object = _UNSET,
        region: str | None | object = _UNSET,
        cloud_type: str | None | object = _UNSET,
        config: JsonObject | None = None,
        priority: int | None = None,
        health_status: str | None = None,
        consecutive_failures: int | None = None,
        cooldown_trips: int | None = None,
        cold_start_p50_ms: int | None | object = _UNSET,
        cold_start_p95_ms: int | None | object = _UNSET,
        recent_error_rate: float | None = None,
        cooldown_until: dt.datetime | None | object = _UNSET,
        source: str | None = None,
        last_applied_yaml_hash: str | None | object = _UNSET,
    ) -> Provider | None:
        sets: list[str] = ["updated_at = now()"]
        params: list[Any] = []
        idx = 1

        field_map: list[tuple[str, Any]] = [
            ("name", name),
            ("provider_type", provider_type),
            ("runpod_endpoint_id", runpod_endpoint_id),
            ("runpod_template_id", runpod_template_id),
            ("region", region),
            ("cloud_type", cloud_type),
            ("config", config),
            ("priority", priority),
            ("health_status", health_status),
            ("consecutive_failures", consecutive_failures),
            ("cooldown_trips", cooldown_trips),
            ("cold_start_p50_ms", cold_start_p50_ms),
            ("cold_start_p95_ms", cold_start_p95_ms),
            ("recent_error_rate", recent_error_rate),
            ("cooldown_until", cooldown_until),
            ("source", source),
            ("last_applied_yaml_hash", last_applied_yaml_hash),
        ]

        for col, val in field_map:
            if val is _UNSET:
                continue
            if val is None and col not in _CLEARABLE_PROVIDER_FIELDS:
                continue
            if col == "config":
                sets.append(f"{col} = ${idx}::jsonb")
            else:
                sets.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1

        params.append(provider_id)
        query = f"UPDATE pitwall.providers SET {', '.join(sets)} WHERE id = ${idx} RETURNING *"

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        if row is None:
            return None
        return _provider_from_row(row)

    async def enable(self, provider_id: str) -> Provider | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE pitwall.providers SET enabled = true, updated_at = now()"
                " WHERE id = $1 RETURNING *",
                provider_id,
            )
        if row is None:
            return None
        return _provider_from_row(row)

    async def disable(self, provider_id: str) -> Provider | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE pitwall.providers SET enabled = false, updated_at = now()"
                " WHERE id = $1 RETURNING *",
                provider_id,
            )
        if row is None:
            return None
        return _provider_from_row(row)


async def insert_audit(
    pool: asyncpg.Pool,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    old_value: JsonObject | None = None,
    new_value: JsonObject | None = None,
    change_reason: str | None = None,
) -> ConfigAuditEntry:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO pitwall.config_audit
                (actor, action, entity_type, entity_id, old_value, new_value,
                 change_reason)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7)
            RETURNING *
            """,
            actor,
            action,
            entity_type,
            entity_id,
            old_value,
            new_value,
            change_reason,
        )
    assert row is not None
    return _audit_from_row(row)


async def list_audit(
    pool: asyncpg.Pool,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    limit: int = 50,
) -> list[ConfigAuditEntry]:
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if entity_type is not None:
        conditions.append(f"entity_type = ${idx}")
        params.append(entity_type)
        idx += 1
    if entity_id is not None:
        conditions.append(f"entity_id = ${idx}")
        params.append(entity_id)
        idx += 1
    if action is not None:
        conditions.append(f"action = ${idx}")
        params.append(action)
        idx += 1

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    query = (
        f"SELECT * FROM pitwall.config_audit{where}"
        f" ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
    )
    params.extend([limit, 0])

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [_audit_from_row(r) for r in rows]


class LeaseRepository:
    """Async repository for ``pitwall.leases``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(
        self,
        lease: Lease,
    ) -> Lease:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO pitwall.leases
                    (id, provider_id, runpod_pod_id, state, created_at,
                     expires_at, renewal_policy, auto_teardown_on_expiry,
                     endpoints, readiness, cost_accrued_usd, last_health_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb,
                        $11, $12)
                RETURNING *
                """,
                lease.id,
                lease.provider_id,
                lease.runpod_pod_id,
                lease.state.value if hasattr(lease.state, "value") else lease.state,
                lease.created_at,
                lease.expires_at,
                lease.renewal_policy.value
                if hasattr(lease.renewal_policy, "value")
                else lease.renewal_policy,
                lease.auto_teardown_on_expiry,
                lease.endpoints.model_dump_json() if lease.endpoints is not None else None,
                lease.readiness.model_dump_json() if lease.readiness is not None else None,
                lease.cost_accrued_usd,
                lease.last_health_at,
            )
        assert row is not None
        return _lease_from_row(row)

    async def get(self, lease_id: str) -> Lease | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM pitwall.leases WHERE id = $1",
                lease_id,
            )
        if row is None:
            return None
        return _lease_from_row(row)

    async def update_state(
        self,
        lease_id: str,
        state: str,
    ) -> Lease | None:
        state_value = state.value if hasattr(state, "value") else state
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE pitwall.leases SET state = $1 WHERE id = $2 RETURNING *",
                state_value,
                lease_id,
            )
        if row is None:
            return None
        return _lease_from_row(row)

    async def close_teardown(
        self,
        lease_id: str,
        *,
        state: str,
        cost_accrued_usd: Any,
        terminated_at: dt.datetime,
        terminated_reason: str,
    ) -> Lease | None:
        state_value = state.value if hasattr(state, "value") else state
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE pitwall.leases
                SET state = $1,
                    cost_accrued_usd = $2,
                    terminated_at = $3,
                    terminated_reason = $4
                WHERE id = $5
                RETURNING *
                """,
                state_value,
                cost_accrued_usd,
                terminated_at,
                terminated_reason,
                lease_id,
            )
        if row is None:
            return None
        return _lease_from_row(row)

    async def update_endpoints(
        self,
        lease_id: str,
        endpoints: LeaseEndpoints,
    ) -> Lease | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE pitwall.leases
                SET endpoints = $1::jsonb
                WHERE id = $2
                RETURNING *
                """,
                endpoints.model_dump_json(),
                lease_id,
            )
        if row is None:
            return None
        return _lease_from_row(row)

    async def update_readiness(
        self,
        lease_id: str,
        readiness: LeaseReadiness,
    ) -> Lease | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE pitwall.leases
                SET readiness = $1::jsonb
                WHERE id = $2
                RETURNING *
                """,
                readiness.model_dump_json(),
                lease_id,
            )
        if row is None:
            return None
        return _lease_from_row(row)

    async def update_expires_at(
        self,
        lease_id: str,
        expires_at: dt.datetime,
    ) -> Lease | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE pitwall.leases
                SET expires_at = $1
                WHERE id = $2
                RETURNING *
                """,
                expires_at,
                lease_id,
            )
        if row is None:
            return None
        return _lease_from_row(row)

    async def patch_settings(
        self,
        lease_id: str,
        *,
        renewal_policy: str | object = _UNSET,
        auto_teardown_on_expiry: bool | object = _UNSET,
        actor: str,
        idempotency_key: str | None = None,
        request_hash: str,
    ) -> LeaseMutationResult | None:
        """Atomically update supported lease settings and write an audit record."""

        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM pitwall.leases WHERE id = $1 FOR UPDATE",
                lease_id,
            )
            if row is None:
                return None
            replayed = await _reserve_lease_mutation(
                conn,
                idempotency_key=idempotency_key,
                lease_id=lease_id,
                operation="patch",
                request_hash=request_hash,
                actor=actor,
            )
            if replayed:
                return LeaseMutationResult(_lease_from_row(row), replayed=True)

            state = str(row["state"])
            if state not in _MUTABLE_LEASE_STATES:
                raise LeaseMutationStateError(state, "patch")

            sets: list[str] = []
            params: list[Any] = []
            old_value: JsonObject = {}
            new_value: JsonObject = {}
            if renewal_policy is not _UNSET:
                policy_value = str(
                    renewal_policy.value if hasattr(renewal_policy, "value") else renewal_policy
                )
                sets.append(f"renewal_policy = ${len(params) + 1}")
                params.append(policy_value)
                old_value["renewal_policy"] = str(row["renewal_policy"])
                new_value["renewal_policy"] = policy_value
            if auto_teardown_on_expiry is not _UNSET:
                teardown_value = bool(auto_teardown_on_expiry)
                sets.append(f"auto_teardown_on_expiry = ${len(params) + 1}")
                params.append(teardown_value)
                old_value["auto_teardown_on_expiry"] = bool(
                    row.get("auto_teardown_on_expiry", True)
                )
                new_value["auto_teardown_on_expiry"] = teardown_value

            if not sets:
                return LeaseMutationResult(_lease_from_row(row))

            params.append(lease_id)
            updated = await conn.fetchrow(
                f"UPDATE pitwall.leases SET {', '.join(sets)} "
                f"WHERE id = ${len(params)} RETURNING *",
                *params,
            )
            assert updated is not None
            await _insert_lease_mutation_audit(
                conn,
                actor=actor,
                action="patch",
                lease_id=lease_id,
                old_value=old_value,
                new_value=new_value,
                idempotency_key=idempotency_key,
            )
            return LeaseMutationResult(_lease_from_row(updated))

    async def renew(
        self,
        lease_id: str,
        *,
        extends_minutes: int,
        actor: str,
        idempotency_key: str | None = None,
        request_hash: str,
        max_horizon_minutes: int,
    ) -> LeaseMutationResult | None:
        """Atomically extend expiry from its current value without lost updates."""

        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM pitwall.leases WHERE id = $1 FOR UPDATE",
                lease_id,
            )
            if row is None:
                return None
            replayed = await _reserve_lease_mutation(
                conn,
                idempotency_key=idempotency_key,
                lease_id=lease_id,
                operation="renew",
                request_hash=request_hash,
                actor=actor,
            )
            if replayed:
                return LeaseMutationResult(_lease_from_row(row), replayed=True)

            state = str(row["state"])
            if state not in _MUTABLE_LEASE_STATES:
                raise LeaseMutationStateError(state, "renew")

            updated = await conn.fetchrow(
                """
                UPDATE pitwall.leases
                SET expires_at = expires_at + make_interval(mins => $1)
                WHERE id = $2
                  AND expires_at + make_interval(mins => $1)
                      <= now() + make_interval(mins => $3)
                RETURNING *
                """,
                extends_minutes,
                lease_id,
                max_horizon_minutes,
            )
            if updated is None:
                raise LeaseMutationExpiryLimitError(lease_id)

            old_expires_at = row["expires_at"]
            new_expires_at = updated["expires_at"]
            await _insert_lease_mutation_audit(
                conn,
                actor=actor,
                action="renew",
                lease_id=lease_id,
                old_value={"expires_at": old_expires_at.isoformat()},
                new_value={
                    "expires_at": new_expires_at.isoformat(),
                    "extends_minutes": extends_minutes,
                },
                idempotency_key=idempotency_key,
            )
            return LeaseMutationResult(_lease_from_row(updated))


_MUTABLE_LEASE_STATES = frozenset({"creating", "waiting_runtime", "waiting_probe", "active"})


async def _reserve_lease_mutation(
    conn: asyncpg.Connection,
    *,
    idempotency_key: str | None,
    lease_id: str,
    operation: str,
    request_hash: str,
    actor: str,
) -> bool:
    """Reserve a mutation key in the caller's transaction; return whether replayed."""

    if idempotency_key is None:
        return False
    inserted = await conn.fetchrow(
        """
        INSERT INTO pitwall.lease_mutation_idempotency
            (idempotency_key, lease_id, operation, request_hash, actor)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING idempotency_key
        """,
        idempotency_key,
        lease_id,
        operation,
        request_hash,
        actor,
    )
    if inserted is not None:
        return False
    existing = await conn.fetchrow(
        """
        SELECT lease_id, operation, request_hash
        FROM pitwall.lease_mutation_idempotency
        WHERE idempotency_key = $1
        """,
        idempotency_key,
    )
    if existing is None or (
        existing["lease_id"] != lease_id
        or existing["operation"] != operation
        or existing["request_hash"] != request_hash
    ):
        raise LeaseMutationIdempotencyError(idempotency_key)
    return True


async def _insert_lease_mutation_audit(
    conn: asyncpg.Connection,
    *,
    actor: str,
    action: str,
    lease_id: str,
    old_value: JsonObject,
    new_value: JsonObject,
    idempotency_key: str | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO pitwall.config_audit
            (actor, action, entity_type, entity_id, old_value, new_value,
             change_reason)
        VALUES ($1, $2, 'lease', $3, $4::jsonb, $5::jsonb, $6)
        """,
        actor,
        action,
        lease_id,
        old_value,
        new_value,
        f"idempotency_key={idempotency_key}" if idempotency_key else None,
    )


def _workload_from_row(row: asyncpg.Record) -> Workload:
    return Workload(
        id=row["id"],
        capability_id=row["capability_id"],
        provider_id=row["provider_id"],
        type=row["type"],
        state=row["state"],
        runpod_job_id=row.get("runpod_job_id"),
        idempotency_key=row.get("idempotency_key"),
        input=row.get("input") if isinstance(row.get("input"), dict) else None,
        result=row.get("result") if isinstance(row.get("result"), dict) else None,
        fallback_chain=list(row["fallback_chain"]) if row.get("fallback_chain") else [],
        error=row.get("error") if isinstance(row.get("error"), dict) else None,
        submitted_at=row["submitted_at"],
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        execution_ms=row.get("execution_ms"),
        queue_ms=row.get("queue_ms"),
        cold_start_ms=row.get("cold_start_ms"),
        input_bytes=row.get("input_bytes"),
        output_bytes=row.get("output_bytes"),
        cost_estimate_usd=row.get("cost_estimate_usd"),
        cost_actual_usd=row.get("cost_actual_usd"),
        langfuse_trace_id=row.get("langfuse_trace_id"),
    )


class WorkloadRepository:
    """Async repository for ``pitwall.workloads``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert(self, workload: Workload) -> Workload:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO pitwall.workloads (
                    id, capability_id, provider_id, type, state,
                    runpod_job_id, idempotency_key, input, result,
                    fallback_chain, error,
                    submitted_at, started_at, completed_at,
                    execution_ms, queue_ms, cold_start_ms,
                    input_bytes, output_bytes,
                    cost_estimate_usd, cost_actual_usd,
                    langfuse_trace_id
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8::jsonb, $9::jsonb,
                    $10, $11::jsonb,
                    $12, $13, $14,
                    $15, $16, $17,
                    $18, $19,
                    $20, $21,
                    $22
                ) RETURNING *
                """,
                workload.id,
                workload.capability_id,
                workload.provider_id,
                workload.type,
                workload.state.value if hasattr(workload.state, "value") else workload.state,
                workload.runpod_job_id,
                workload.idempotency_key,
                workload.input,
                workload.result,
                workload.fallback_chain if workload.fallback_chain else None,
                workload.error,
                workload.submitted_at,
                workload.started_at,
                workload.completed_at,
                workload.execution_ms,
                workload.queue_ms,
                workload.cold_start_ms,
                workload.input_bytes,
                workload.output_bytes,
                workload.cost_estimate_usd,
                workload.cost_actual_usd,
                workload.langfuse_trace_id,
            )
        assert row is not None
        return _workload_from_row(row)

    async def get(self, workload_id: str) -> Workload | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM pitwall.workloads WHERE id = $1",
                workload_id,
            )
        if row is None:
            return None
        return _workload_from_row(row)

    async def get_by_idempotency_key(self, key: str) -> Workload | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM pitwall.workloads WHERE idempotency_key = $1",
                key,
            )
        if row is None:
            return None
        return _workload_from_row(row)

    async def update_state(
        self,
        workload_id: str,
        state: str | WorkloadState,
        *,
        started_at: dt.datetime | None = None,
        completed_at: dt.datetime | None = None,
        execution_ms: int | None = None,
        queue_ms: int | None = None,
        result: JsonObject | None = None,
        error: JsonObject | None = None,
        output_bytes: int | None = None,
        fallback_chain: list[str] | None = None,
        langfuse_trace_id: str | None = None,
    ) -> Workload | None:
        state_value = state.value if hasattr(state, "value") else state
        sets: list[str] = ["state = $1"]
        params: list[Any] = [state_value]
        idx = 2

        if started_at is not None:
            sets.append(f"started_at = ${idx}")
            params.append(started_at)
            idx += 1
        if completed_at is not None:
            sets.append(f"completed_at = ${idx}")
            params.append(completed_at)
            idx += 1
        if execution_ms is not None:
            sets.append(f"execution_ms = ${idx}")
            params.append(execution_ms)
            idx += 1
        if queue_ms is not None:
            sets.append(f"queue_ms = ${idx}")
            params.append(queue_ms)
            idx += 1
        if result is not None:
            sets.append(f"result = ${idx}::jsonb")
            params.append(result)
            idx += 1
        if error is not None:
            sets.append(f"error = ${idx}::jsonb")
            params.append(error)
            idx += 1
        if output_bytes is not None:
            sets.append(f"output_bytes = ${idx}")
            params.append(output_bytes)
            idx += 1
        if fallback_chain is not None:
            sets.append(f"fallback_chain = ${idx}")
            params.append(fallback_chain if fallback_chain else None)
            idx += 1
        if langfuse_trace_id is not None:
            sets.append(f"langfuse_trace_id = ${idx}")
            params.append(langfuse_trace_id)
            idx += 1

        params.append(workload_id)
        query = f"UPDATE pitwall.workloads SET {', '.join(sets)} WHERE id = ${idx} RETURNING *"

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        if row is None:
            return None
        return _workload_from_row(row)

    async def guarded_transition(
        self,
        workload_id: str,
        from_states: set[str],
        to_state: str | WorkloadState,
        *,
        patch: dict[str, Any] | None = None,
    ) -> Workload | None:
        """Atomically transition a workload with a row lock and state guard.

        Acquires a ``SELECT ... FOR UPDATE`` row lock, then performs an
        ``UPDATE ... WHERE state IN (from_states)`` so concurrent retries
        cannot double-submit a workload.

        Returns the updated Workload on success, or ``None`` if the workload
        was not in one of the allowed *from_states*.
        """
        to_state_value = to_state.value if hasattr(to_state, "value") else to_state
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT 1 FROM pitwall.workloads WHERE id = $1 FOR UPDATE",
                workload_id,
            )
            set_clauses: list[str] = ["state = $1"]
            params: list[Any] = [to_state_value]
            idx = 2

            if patch:
                for col, val in patch.items():
                    if col in _WORKLOAD_JSONB_COLUMNS:
                        set_clauses.append(f"{col} = ${idx}::jsonb")
                    else:
                        set_clauses.append(f"{col} = ${idx}")
                    params.append(val)
                    idx += 1

            params.append(workload_id)
            workload_param_idx = idx
            idx += 1

            state_placeholders = ", ".join(f"${idx + i}" for i in range(len(from_states)))
            params.extend(sorted(from_states))

            query = (
                f"UPDATE pitwall.workloads SET {', '.join(set_clauses)} "
                f"WHERE id = ${workload_param_idx} "
                f"AND state IN ({state_placeholders}) "
                f"RETURNING *"
            )

            row = await conn.fetchrow(query, *params)
            if row is None:
                return None
            return _workload_from_row(row)


_INSERT_WEBHOOK_DELIVERY_SQL = """
    INSERT INTO pitwall.runpod_webhook_deliveries (runpod_job_id, attempt, payload)
    VALUES ($1, $2, $3::jsonb)
    ON CONFLICT (runpod_job_id, attempt) DO NOTHING
    RETURNING id
"""


@dataclass(frozen=True)
class WebhookDeliveryResult:
    is_new: bool
    delivery_id: int | None


class WebhookDeliveryRepository:
    """Async repository for ``pitwall.runpod_webhook_deliveries``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert_or_skip(
        self,
        runpod_job_id: str,
        attempt: int,
        payload: dict[str, Any],
    ) -> WebhookDeliveryResult:
        """Insert a webhook delivery record, skipping if (runpod_job_id, attempt) already exists.

        Uses the UNIQUE (runpod_job_id, attempt) constraint as a dedupe gate.
        Returns WebhookDeliveryResult(is_new=True, ...) if inserted, or
        WebhookDeliveryResult(is_new=False, ...) if skipped due to conflict.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                _INSERT_WEBHOOK_DELIVERY_SQL,
                runpod_job_id,
                attempt,
                payload,
            )
        if row is not None:
            return WebhookDeliveryResult(is_new=True, delivery_id=row["id"])
        return WebhookDeliveryResult(is_new=False, delivery_id=None)


def _webhook_delivery_failure_from_row(row: asyncpg.Record) -> WebhookDeliveryFailure:
    return WebhookDeliveryFailure(
        id=row["id"],
        workload_id=row["workload_id"],
        subscription_id=row["subscription_id"],
        attempt=row["attempt"],
        attempted_at=row["attempted_at"],
        next_retry_at=row.get("next_retry_at"),
        payload=row["payload"],
        status_code=row.get("status_code"),
        error_message=row.get("error_message"),
    )


class WebhookDeliveryFailureRepository:
    """Async repository for ``pitwall.webhook_delivery_failures``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert(
        self,
        workload_id: str,
        subscription_id: int,
        attempt: int,
        payload: dict[str, Any],
        *,
        next_retry_at: dt.datetime | None = None,
        status_code: int | None = None,
        error_message: str | None = None,
    ) -> WebhookDeliveryFailure:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO pitwall.webhook_delivery_failures
                    (workload_id, subscription_id, attempt, next_retry_at,
                     payload, status_code, error_message)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                RETURNING *
                """,
                workload_id,
                subscription_id,
                attempt,
                next_retry_at,
                payload,
                status_code,
                error_message,
            )
        assert row is not None
        return _webhook_delivery_failure_from_row(row)

    async def get(
        self,
        workload_id: str,
        subscription_id: int,
        attempt: int,
    ) -> WebhookDeliveryFailure | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM pitwall.webhook_delivery_failures
                WHERE workload_id = $1 AND subscription_id = $2 AND attempt = $3
                """,
                workload_id,
                subscription_id,
                attempt,
            )
        if row is None:
            return None
        return _webhook_delivery_failure_from_row(row)

    async def list_by_workload(
        self,
        workload_id: str,
    ) -> list[WebhookDeliveryFailure]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM pitwall.webhook_delivery_failures
                WHERE workload_id = $1
                ORDER BY attempt ASC
                """,
                workload_id,
            )
        return [_webhook_delivery_failure_from_row(r) for r in rows]

    async def list_pending_retries(
        self,
        before: dt.datetime,
        limit: int = 100,
    ) -> list[WebhookDeliveryFailure]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM pitwall.webhook_delivery_failures
                WHERE next_retry_at IS NOT NULL AND next_retry_at <= $1
                ORDER BY next_retry_at ASC
                LIMIT $2
                """,
                before,
                limit,
            )
        return [_webhook_delivery_failure_from_row(r) for r in rows]

    async def update_next_retry(
        self,
        workload_id: str,
        subscription_id: int,
        attempt: int,
        next_retry_at: dt.datetime | None,
    ) -> WebhookDeliveryFailure | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE pitwall.webhook_delivery_failures
                SET next_retry_at = $4
                WHERE workload_id = $1 AND subscription_id = $2 AND attempt = $3
                RETURNING *
                """,
                workload_id,
                subscription_id,
                attempt,
                next_retry_at,
            )
        if row is None:
            return None
        return _webhook_delivery_failure_from_row(row)


def _webhook_subscription_from_row(
    row: asyncpg.Record,
    cipher: WebhookSecretCipher | None = None,
) -> WebhookSubscription:
    hmac_secret: str | None = None
    ciphertext = row.get("hmac_secret_ciphertext")
    nonce = row.get("hmac_secret_nonce")
    key_version = row.get("hmac_secret_key_version")
    if cipher is not None and ciphertext is not None and nonce is not None and key_version:
        hmac_secret = cipher.decrypt(
            EncryptedWebhookSecret(
                ciphertext=bytes(ciphertext),
                nonce=bytes(nonce),
                key_version=str(key_version),
            )
        )
    return WebhookSubscription(
        id=str(row["id"]),
        consumer=row["consumer"],
        webhook_url=row["webhook_url"],
        hmac_secret=hmac_secret,
        active=row.get("active", True),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class WebhookSubscriptionRepository:
    """Async repository for ``pitwall.webhook_subscriptions``."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        cipher: WebhookSecretCipher | None = None,
    ) -> None:
        self._pool = pool
        self._cipher = cipher

    async def create(
        self,
        consumer: str,
        webhook_url: str,
        *,
        hmac_secret: str,
        active: bool = True,
        actor: str = "rest:webhook",
    ) -> WebhookSubscription:
        if self._cipher is None:
            raise RuntimeError("webhook secret encryption is not configured")
        encrypted = self._cipher.encrypt(hmac_secret)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO pitwall.webhook_subscriptions
                    (consumer, webhook_url, hmac_secret_ciphertext,
                     hmac_secret_nonce, hmac_secret_key_version, active)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
                """,
                consumer,
                webhook_url,
                encrypted.ciphertext,
                encrypted.nonce,
                encrypted.key_version,
                active,
            )
            assert row is not None
            await _insert_webhook_subscription_audit(
                conn,
                actor=actor,
                action="create",
                subscription_id=str(row["id"]),
                old_value=None,
                new_value={"consumer": consumer, "active": active},
            )
        return _webhook_subscription_from_row(row, self._cipher)

    async def get(self, subscription_id: int) -> WebhookSubscription | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM pitwall.webhook_subscriptions WHERE id = $1",
                subscription_id,
            )
        if row is None:
            return None
        return _webhook_subscription_from_row(row)

    async def list(
        self,
        *,
        consumer: str | None = None,
        active_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> builtins.list[WebhookSubscription]:
        conditions: list[str] = []
        params: list[Any] = []
        idx = 1

        if consumer is not None:
            conditions.append(f"consumer = ${idx}")
            params.append(consumer)
            idx += 1
        if active_only:
            conditions.append("active = true")

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = (
            f"SELECT * FROM pitwall.webhook_subscriptions{where}"
            f" ORDER BY id LIMIT ${idx} OFFSET ${idx + 1}"
        )
        params.extend([limit, offset])

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [_webhook_subscription_from_row(r) for r in rows]

    async def list_for_dispatch(
        self,
        *,
        consumer: str,
        limit: int = 100,
    ) -> builtins.list[WebhookSubscription]:
        if self._cipher is None:
            raise RuntimeError("webhook secret encryption is not configured")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM pitwall.webhook_subscriptions
                WHERE consumer = $1 AND active = true
                ORDER BY id LIMIT $2
                """,
                consumer,
                limit,
            )
        return [_webhook_subscription_from_row(row, self._cipher) for row in rows]

    async def rotate_secret(
        self,
        subscription_id: int,
        hmac_secret: str,
        *,
        actor: str = "rest:webhook",
    ) -> WebhookSubscription | None:
        if self._cipher is None:
            raise RuntimeError("webhook secret encryption is not configured")
        encrypted = self._cipher.encrypt(hmac_secret)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE pitwall.webhook_subscriptions
                SET hmac_secret_ciphertext = $1,
                    hmac_secret_nonce = $2,
                    hmac_secret_key_version = $3,
                    active = true,
                    updated_at = now()
                WHERE id = $4
                RETURNING *
                """,
                encrypted.ciphertext,
                encrypted.nonce,
                encrypted.key_version,
                subscription_id,
            )
            if row is None:
                return None
            await _insert_webhook_subscription_audit(
                conn,
                actor=actor,
                action="rotate",
                subscription_id=str(subscription_id),
                old_value=None,
                new_value={"secret_rotated": True, "active": True},
            )
        return _webhook_subscription_from_row(row, self._cipher)

    async def deactivate(
        self,
        subscription_id: int,
        *,
        actor: str = "rest:webhook",
    ) -> WebhookSubscription | None:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE pitwall.webhook_subscriptions
                SET active = false, updated_at = now()
                WHERE id = $1
                RETURNING *
                """,
                subscription_id,
            )
            if row is None:
                return None
            await _insert_webhook_subscription_audit(
                conn,
                actor=actor,
                action="deactivate",
                subscription_id=str(subscription_id),
                old_value={"active": True},
                new_value={"active": False},
            )
        return _webhook_subscription_from_row(row)

    async def activate(
        self,
        subscription_id: int,
        *,
        actor: str = "rest:webhook",
    ) -> WebhookSubscription | None:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE pitwall.webhook_subscriptions
                SET active = true, updated_at = now()
                WHERE id = $1
                  AND hmac_secret_ciphertext IS NOT NULL
                  AND hmac_secret_nonce IS NOT NULL
                  AND hmac_secret_key_version IS NOT NULL
                RETURNING *
                """,
                subscription_id,
            )
            if row is None:
                return None
            await _insert_webhook_subscription_audit(
                conn,
                actor=actor,
                action="activate",
                subscription_id=str(subscription_id),
                old_value={"active": False},
                new_value={"active": True},
            )
        return _webhook_subscription_from_row(row)

    async def delete(
        self,
        subscription_id: int,
        *,
        actor: str = "rest:webhook",
    ) -> bool:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "DELETE FROM pitwall.webhook_subscriptions WHERE id = $1 RETURNING id, consumer",
                subscription_id,
            )
            if row is None:
                return False
            await _insert_webhook_subscription_audit(
                conn,
                actor=actor,
                action="delete",
                subscription_id=str(subscription_id),
                old_value={"consumer": str(row["consumer"])},
                new_value=None,
            )
        return True


async def _insert_webhook_subscription_audit(
    conn: asyncpg.Connection,
    *,
    actor: str,
    action: str,
    subscription_id: str,
    old_value: JsonObject | None,
    new_value: JsonObject | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO pitwall.config_audit
            (actor, action, entity_type, entity_id, old_value, new_value)
        VALUES ($1, $2, 'webhook_subscription', $3, $4::jsonb, $5::jsonb)
        """,
        actor,
        action,
        subscription_id,
        old_value,
        new_value,
    )


__all__ = [
    "CapabilityRepository",
    "ConfigAuditEntry",
    "LeaseRepository",
    "LEASE_MUTATION_UNSET",
    "LeaseMutationExpiryLimitError",
    "LeaseMutationIdempotencyError",
    "LeaseMutationResult",
    "LeaseMutationStateError",
    "WebhookDeliveryFailureRepository",
    "WebhookDeliveryRepository",
    "WebhookDeliveryResult",
    "WebhookSubscriptionRepository",
    "WorkloadRepository",
    "insert_audit",
    "list_audit",
]
