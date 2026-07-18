"""Atomic cost admission gate for Pitwall workloads."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol, cast, runtime_checkable

log = logging.getLogger("pitwall.cost.budget_gate")

PITWALL_BUDGET_LOCK_KEY = int.from_bytes(b"PITWBUDG", "big")
PITWALL_BUDGET_GATE_LOCK_KEY = PITWALL_BUDGET_LOCK_KEY

BudgetRejectionReason = Literal["monthly_budget", "per_request_cap"]


@runtime_checkable
class BudgetEstimate(Protocol):
    """Admission estimate object that can provide a pre-spend upper bound."""

    def upper_bound(self) -> Decimal: ...


type BudgetEstimateInput = Decimal | str | int | float | BudgetEstimate


@dataclass(frozen=True)
class BudgetSnapshot:
    """Budget state captured at the point a launch is rejected."""

    monthly_budget_usd: Decimal
    per_request_max_usd: Decimal
    mtd_spend_usd: Decimal
    estimate_usd: Decimal
    budget_remaining_usd: Decimal

    def model_dump(
        self, *, mode: Literal["python", "json"] = "python", **_: Any
    ) -> dict[str, Decimal] | dict[str, str]:
        data = asdict(self)
        if mode == "python":
            return data
        if mode == "json":
            return {key: str(value) for key, value in data.items()}
        raise ValueError(f"unsupported dump mode: {mode!r}")

    def to_serializable_dict(self) -> dict[str, str]:
        """Return a stdlib-JSON-safe snapshot for HTTP response bodies."""

        return cast(dict[str, str], self.model_dump(mode="json"))

    def model_dump_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_serializable_dict(), **kwargs)


@dataclass(frozen=True)
class BudgetAdmission:
    """Result of an admission attempt under the budget lock."""

    workload_id: str
    is_new: bool


class BudgetRejected(RuntimeError):
    """Raised when a workload cannot be admitted under the configured budget."""

    error_code = "budget_rejected"
    status_code = 402

    def __init__(self, reason: BudgetRejectionReason, snapshot: BudgetSnapshot) -> None:
        super().__init__(reason)
        self.reason = reason
        self.snapshot = snapshot

    def to_response_body(self) -> dict[str, Any]:
        """Return the canonical HTTP 402 response body."""

        return {
            "error": self.error_code,
            "reason": self.reason,
            "snapshot": self.snapshot.to_serializable_dict(),
        }

    def to_http_response_body(self) -> dict[str, Any]:
        return self.to_response_body()


class BudgetGate:
    """Postgres-backed whole-account budget admission gate."""

    def __init__(
        self,
        pool: Any,
        *,
        monthly_budget_usd: Decimal | str | int | float | None = None,
        per_request_max_usd: Decimal | str | int | float | None = None,
        workload_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.pool = pool
        self.monthly_budget_usd = _positive_decimal(
            monthly_budget_usd
            if monthly_budget_usd is not None
            else _required_env("PITWALL_MONTHLY_BUDGET_USD"),
            "monthly_budget_usd",
        )
        self.per_request_max_usd = _positive_decimal(
            per_request_max_usd
            if per_request_max_usd is not None
            else _required_env("PITWALL_PER_REQUEST_MAX_USD"),
            "per_request_max_usd",
        )
        self._workload_id_factory = workload_id_factory or _new_workload_id

    async def current_mtd_spend(self) -> Decimal:
        """Return month-to-date spend across all admitted workloads.

        Read-only query (no advisory lock). Mirrors the spend window used by
        admission so callers can inspect budget state without contending for
        the lock. Terminal failures, cancellations, and timeouts still count:
        admitted provider spend remains billable even when the workload fails.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT COALESCE(SUM(COALESCE(cost_actual_usd, cost_estimate_usd)), 0) AS s
                   FROM pitwall.workloads
                   WHERE submitted_at >= date_trunc('month', now() AT TIME ZONE 'UTC')"""
            )
        return _decimal_from_row(row, "s")

    async def try_launch(
        self,
        *,
        capability_id: str,
        provider_id: str,
        estimate_usd: BudgetEstimateInput,
        workload_type: str = "inference",
        submitted_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Admit a workload under a Postgres advisory lock and return its id.

        When *idempotency_key* is provided and a workload with that key already
        exists, the existing workload id is returned without inserting a
        duplicate row.  This preserves the "create before queue dispatch"
        invariant for async jobs while keeping idempotency semantics intact.
        """
        admission = await self.try_launch_admission(
            capability_id=capability_id,
            provider_id=provider_id,
            estimate_usd=estimate_usd,
            workload_type=workload_type,
            submitted_at=submitted_at,
            idempotency_key=idempotency_key,
        )
        return admission.workload_id

    async def try_launch_admission(
        self,
        *,
        capability_id: str,
        provider_id: str,
        estimate_usd: BudgetEstimateInput,
        workload_type: str = "inference",
        submitted_at: datetime | None = None,
        idempotency_key: str | None = None,
        before_new_admission: Callable[[], Awaitable[None]] | None = None,
    ) -> BudgetAdmission:
        """Admit a workload and report whether this call inserted the row."""

        estimate = _positive_estimate(estimate_usd)
        submitted = submitted_at or datetime.now(UTC)

        if idempotency_key is None and estimate > self.per_request_max_usd:
            log.warning(
                "per-request cap exceeded: %.6f > %.6f",
                estimate,
                self.per_request_max_usd,
            )
            raise BudgetRejected(
                "per_request_cap",
                self._snapshot(mtd_spend_usd=Decimal("0"), estimate_usd=estimate),
            )

        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)",
                PITWALL_BUDGET_LOCK_KEY,
            )
            if idempotency_key is not None:
                existing_id = await conn.fetchval(
                    "SELECT id FROM pitwall.workloads WHERE idempotency_key = $1",
                    idempotency_key,
                )
                if existing_id is not None:
                    log.info(
                        "idempotency key hit: returning existing workload %s",
                        existing_id,
                    )
                    return BudgetAdmission(workload_id=str(existing_id), is_new=False)

            if before_new_admission is not None:
                await before_new_admission()

            if estimate > self.per_request_max_usd:
                log.warning(
                    "per-request cap exceeded: %.6f > %.6f",
                    estimate,
                    self.per_request_max_usd,
                )
                raise BudgetRejected(
                    "per_request_cap",
                    self._snapshot(mtd_spend_usd=Decimal("0"), estimate_usd=estimate),
                )

            row = await conn.fetchrow(
                """SELECT COALESCE(SUM(COALESCE(cost_actual_usd, cost_estimate_usd)), 0) AS s
                       FROM pitwall.workloads
                       WHERE submitted_at >= date_trunc('month', now() AT TIME ZONE 'UTC')"""
            )
            spend = _decimal_from_row(row, "s")
            if spend + estimate > self.monthly_budget_usd:
                log.warning(
                    "monthly budget would exceed under advisory lock: %.6f + %.6f > %.6f",
                    spend,
                    estimate,
                    self.monthly_budget_usd,
                )
                raise BudgetRejected(
                    "monthly_budget",
                    self._snapshot(mtd_spend_usd=spend, estimate_usd=estimate),
                )
            workload_id = self._workload_id_factory()
            if idempotency_key is None:
                admitted_id = await conn.fetchval(
                    """INSERT INTO pitwall.workloads (
                               id, capability_id, provider_id, type, state,
                               cost_estimate_usd, submitted_at
                           )
                           VALUES ($1, $2, $3, $4, 'queued', $5, $6)
                           RETURNING id""",
                    workload_id,
                    capability_id,
                    provider_id,
                    workload_type,
                    estimate,
                    submitted,
                )
            else:
                admitted_id = await conn.fetchval(
                    """INSERT INTO pitwall.workloads (
                               id, capability_id, provider_id, type, state,
                               cost_estimate_usd, submitted_at, idempotency_key
                           )
                           VALUES ($1, $2, $3, $4, 'queued', $5, $6, $7)
                           RETURNING id""",
                    workload_id,
                    capability_id,
                    provider_id,
                    workload_type,
                    estimate,
                    submitted,
                    idempotency_key,
                )
            return BudgetAdmission(workload_id=str(admitted_id), is_new=True)

    def _snapshot(self, *, mtd_spend_usd: Decimal, estimate_usd: Decimal) -> BudgetSnapshot:
        remaining = self.monthly_budget_usd - mtd_spend_usd
        return BudgetSnapshot(
            monthly_budget_usd=self.monthly_budget_usd,
            per_request_max_usd=self.per_request_max_usd,
            mtd_spend_usd=mtd_spend_usd,
            estimate_usd=estimate_usd,
            budget_remaining_usd=remaining if remaining > 0 else Decimal("0"),
        )


def _new_workload_id() -> str:
    from pitwall.core.ids import ulid_new

    return f"wkl_{ulid_new()}"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} must be set")
    return value


def _positive_decimal(raw_value: Decimal | str | int | float, name: str) -> Decimal:
    value = _decimal(raw_value, name)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_estimate(raw_value: BudgetEstimateInput) -> Decimal:
    if isinstance(raw_value, BudgetEstimate):
        return _positive_decimal(raw_value.upper_bound(), "estimate_usd")
    return _positive_decimal(raw_value, "estimate_usd")


def _decimal(raw_value: Any, name: str) -> Decimal:
    if isinstance(raw_value, bool):
        raise ValueError(f"{name} must be a decimal value")
    try:
        value = Decimal(str(raw_value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal value") from exc
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _decimal_from_row(row: Mapping[str, Any] | None, key: str) -> Decimal:
    if row is None:
        return Decimal("0")
    return _decimal(row[key], key)


__all__ = [
    "BudgetAdmission",
    "BudgetEstimate",
    "BudgetEstimateInput",
    "BudgetGate",
    "BudgetRejected",
    "BudgetRejectionReason",
    "BudgetSnapshot",
    "PITWALL_BUDGET_GATE_LOCK_KEY",
    "PITWALL_BUDGET_LOCK_KEY",
]
