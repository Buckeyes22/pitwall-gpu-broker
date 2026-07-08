from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import pitwall.core.inference as inference
from pitwall.core.enums import ProviderType, WorkloadState
from pitwall.core.models import Capability, Provider
from pitwall.cost.budget_gate import BudgetRejected

pytestmark = pytest.mark.anyio

_NOW = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.UTC)


@dataclass(frozen=True)
class _Settings:
    runpod_api_key: str | None = "test-key"
    pitwall_monthly_budget_usd: Decimal = Decimal("10")
    pitwall_per_request_max_usd: Decimal = Decimal("5")


@dataclass
class _QueueJob:
    id: str


class _QueueClient:
    def __init__(self, *, run_error: httpx.HTTPError | RuntimeError | None = None) -> None:
        self.run_error = run_error
        self.run_calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def run(
        self,
        endpoint_id: str,
        *,
        input: dict[str, Any],
        webhook: str | None = None,
    ) -> _QueueJob:
        self.run_calls.append((endpoint_id, input, webhook))
        if self.run_error is not None:
            raise self.run_error
        return _QueueJob(id="rp-job-123")


class _Tx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, *, current_spend: Decimal = Decimal("0")) -> None:
        self.current_spend = current_spend
        self.workloads: dict[str, dict[str, Any]] = {}
        self.budget_insert_count = 0
        self.repository_insert_count = 0
        self.conn = _Conn(self)

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)

    def workload_id_for_key(self, idempotency_key: object) -> str | None:
        for row in self.workloads.values():
            if row.get("idempotency_key") == idempotency_key:
                return str(row["id"])
        return None

    def seed_workload(
        self,
        workload_id: str,
        *,
        idempotency_key: str | None = None,
        runpod_job_id: str | None = None,
    ) -> None:
        self.workloads[workload_id] = _workload_row(
            workload_id=workload_id,
            capability_id="cap_async",
            provider_id="prov_async",
            workload_type="async_job",
            state=WorkloadState.QUEUED.value,
            submitted_at=_NOW,
            runpod_job_id=runpod_job_id,
            idempotency_key=idempotency_key,
            cost_estimate_usd=Decimal("0.250000"),
        )


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    def transaction(self) -> _Tx:
        return _Tx()

    async def execute(self, sql: str, *args: object) -> str:
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        if "SET input = $2::jsonb" in sql:
            self._pool.workloads[str(args[0])]["input"] = args[1]
            return "UPDATE 1"
        if "SET runpod_job_id = $2" in sql:
            self._pool.workloads[str(args[0])]["runpod_job_id"] = args[1]
            return "UPDATE 1"
        if sql.startswith("UPDATE pitwall.workloads SET state = $1"):
            workload_id = str(args[-2])
            from_states = {str(state) for state in args[-1:]}
            row = self._pool.workloads[workload_id]
            if row["state"] not in from_states:
                return "UPDATE 0"
            row["state"] = str(args[0])
            row["completed_at"] = args[1]
            row["cost_actual_usd"] = args[2]
            row["error"] = args[3]
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        if "SUM(COALESCE(cost_actual_usd, cost_estimate_usd))" in sql:
            return {"s": self._pool.current_spend}
        if "SELECT * FROM pitwall.workloads WHERE id = $1" in sql:
            return self._pool.workloads.get(str(args[0]))
        if "INSERT INTO pitwall.workloads" in sql:
            self._pool.repository_insert_count += 1
            idempotency_key = args[6]
            if idempotency_key is not None and self._pool.workload_id_for_key(idempotency_key):
                raise AssertionError("repository insert attempted duplicate idempotency key")
            row = _workload_row(
                workload_id=str(args[0]),
                capability_id=str(args[1]),
                provider_id=str(args[2]),
                workload_type=str(args[3]),
                state=str(args[4]),
                runpod_job_id=args[5] if isinstance(args[5], str) else None,
                idempotency_key=idempotency_key if isinstance(idempotency_key, str) else None,
                input_payload=args[7] if isinstance(args[7], dict) else None,
                result=args[8] if isinstance(args[8], dict) else None,
                fallback_chain=list(args[9]) if isinstance(args[9], list) else None,
                error=args[10] if isinstance(args[10], dict) else None,
                submitted_at=args[11] if isinstance(args[11], dt.datetime) else _NOW,
                cost_estimate_usd=args[19] if isinstance(args[19], Decimal) else None,
                cost_actual_usd=args[20] if isinstance(args[20], Decimal) else None,
            )
            self._pool.workloads[row["id"]] = row
            return row
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql: str, *args: object) -> Any:
        if "SELECT id FROM pitwall.workloads WHERE idempotency_key = $1" in sql:
            return self._pool.workload_id_for_key(args[0])
        if "INSERT INTO pitwall.workloads" in sql:
            self._pool.budget_insert_count += 1
            row = _workload_row(
                workload_id=str(args[0]),
                capability_id=str(args[1]),
                provider_id=str(args[2]),
                workload_type=str(args[3]),
                state=WorkloadState.QUEUED.value,
                submitted_at=args[5] if isinstance(args[5], dt.datetime) else _NOW,
                idempotency_key=str(args[6]) if "idempotency_key" in sql else None,
                cost_estimate_usd=args[4] if isinstance(args[4], Decimal) else None,
            )
            self._pool.workloads[row["id"]] = row
            return row["id"]
        raise AssertionError(f"unexpected fetchval SQL: {sql}")


def _workload_row(
    *,
    workload_id: str,
    capability_id: str,
    provider_id: str,
    workload_type: str,
    state: str,
    submitted_at: dt.datetime,
    runpod_job_id: str | None = None,
    idempotency_key: str | None = None,
    input_payload: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    fallback_chain: list[str] | None = None,
    error: dict[str, Any] | None = None,
    cost_estimate_usd: Decimal | None = None,
    cost_actual_usd: Decimal | None = None,
) -> dict[str, Any]:
    return {
        "id": workload_id,
        "capability_id": capability_id,
        "provider_id": provider_id,
        "type": workload_type,
        "state": state,
        "runpod_job_id": runpod_job_id,
        "idempotency_key": idempotency_key,
        "input": input_payload,
        "result": result,
        "fallback_chain": fallback_chain,
        "error": error,
        "submitted_at": submitted_at,
        "started_at": None,
        "completed_at": None,
        "execution_ms": None,
        "queue_ms": None,
        "cold_start_ms": None,
        "input_bytes": None,
        "output_bytes": None,
        "cost_estimate_usd": cost_estimate_usd,
        "cost_actual_usd": cost_actual_usd,
        "langfuse_trace_id": None,
    }


def _capability() -> Capability:
    return Capability(
        id="cap_async",
        name="async.test",
        version="1.0.0",
        class_="embedding",
        cost_mode="per_request",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider() -> Provider:
    return Provider(
        id="prov_async",
        capability_id="cap_async",
        name="Async provider",
        provider_type=ProviderType.SERVERLESS_QUEUE,
        runpod_endpoint_id="ep-async",
        config={"cost": {"kind": "per_request", "per_request": "0.25"}},
        priority=1,
        updated_at=_NOW,
    )


async def test_async_job_budget_rejection_never_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _Pool(current_spend=Decimal("1.00"))
    queue_client = _QueueClient()
    monkeypatch.setattr(inference, "QueueClient", lambda *, api_key: queue_client)

    with pytest.raises(BudgetRejected) as exc_info:
        await inference.create_and_dispatch_job(
            pool,
            capability=_capability(),
            provider=_provider(),
            capability_params={"input": "hello"},
            idempotency_key=None,
            webhook_url=None,
            settings=_Settings(
                pitwall_monthly_budget_usd=Decimal("1.00"),
                pitwall_per_request_max_usd=Decimal("5"),
            ),
        )

    assert exc_info.value.reason == "monthly_budget"
    assert queue_client.run_calls == []
    assert pool.workloads == {}


async def test_async_job_reuses_budget_admission_row_for_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    queue_client = _QueueClient()
    monkeypatch.setattr(inference, "QueueClient", lambda *, api_key: queue_client)
    monkeypatch.setattr(
        inference,
        "resolve_webhook_target",
        AsyncMock(return_value=SimpleNamespace(url="https://hooks.example.test/runpod")),
    )
    payload = {"input": "hello"}

    workload = await inference.create_and_dispatch_job(
        pool,
        capability=_capability(),
        provider=_provider(),
        capability_params=payload,
        idempotency_key=None,
        webhook_url="https://hooks.example.test/runpod",
        settings=_Settings(),
    )

    assert pool.budget_insert_count == 1
    assert pool.repository_insert_count == 0
    assert len(pool.workloads) == 1
    row = pool.workloads[workload.id]
    assert row["cost_estimate_usd"] == Decimal("0.250000")
    assert row["input"] == payload
    assert row["runpod_job_id"] == "rp-job-123"
    assert workload.cost_estimate_usd == Decimal("0.250000")
    assert workload.runpod_job_id == "rp-job-123"
    assert queue_client.run_calls == [("ep-async", payload, "https://hooks.example.test/runpod")]


async def test_async_job_dispatch_failure_closes_admitted_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    dispatch_error = httpx.ConnectError("network down")
    queue_client = _QueueClient(run_error=dispatch_error)
    monkeypatch.setattr(inference, "QueueClient", lambda *, api_key: queue_client)

    with pytest.raises(httpx.ConnectError):
        await inference.create_and_dispatch_job(
            pool,
            capability=_capability(),
            provider=_provider(),
            capability_params={"input": "hello"},
            idempotency_key=None,
            webhook_url=None,
            settings=_Settings(),
        )

    assert pool.budget_insert_count == 1
    assert pool.repository_insert_count == 0
    [row] = pool.workloads.values()
    assert row["state"] == WorkloadState.FAILED.value
    assert row["cost_actual_usd"] == Decimal("0")
    assert row["completed_at"] is not None
    assert row["error"] == {"type": "ConnectError", "message": "network down"}
    assert row["runpod_job_id"] is None


async def test_async_job_idempotency_hit_returns_existing_workload_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    pool.seed_workload(
        "wkl_existing",
        idempotency_key="idem-1",
        runpod_job_id="rp-existing",
    )
    queue_client = _QueueClient()
    monkeypatch.setattr(inference, "QueueClient", lambda *, api_key: queue_client)

    workload = await inference.create_and_dispatch_job(
        pool,
        capability=_capability(),
        provider=_provider(),
        capability_params={"input": "hello"},
        idempotency_key="idem-1",
        webhook_url=None,
        settings=_Settings(),
    )

    assert workload.id == "wkl_existing"
    assert workload.runpod_job_id == "rp-existing"
    assert queue_client.run_calls == []
    assert pool.budget_insert_count == 0
    assert pool.repository_insert_count == 0
