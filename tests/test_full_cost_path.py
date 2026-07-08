"""full hermetic cost path coverage.

This test exercises the E4 cost accounting path end to end without Postgres,
Redis, RunPod, Prometheus scraping, or Resend network calls:

estimate -> budget gate -> RunPod billing reconcile -> daily rollup ->
exporter refresh -> threshold alert notification + record.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from prometheus_client import generate_latest

from pitwall.core import Capability
from pitwall.cost.budget_gate import PITWALL_BUDGET_LOCK_KEY, BudgetGate
from pitwall.cost.sync_gate import gate_sync_inference
from pitwall.cost.threshold_alerts import (
    evaluate_crossings,
    record_crossings,
    send_crossing_notifications,
)
from tests.fakes.runpod import RunPodBillingFake

pytestmark = pytest.mark.anyio

_NOW = dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.UTC)


async def test_full_cost_path_estimate_gate_reconcile_rollup_exporter_and_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pitwall.cost.notifications import NotificationResult
    from pitwall.reconciler import (
        aggregate_daily_cost,
        apply_terminal_state,
        fetch_active_workloads,
        map_runpod_status,
    )

    db = _FakeCostDatabase()
    db.capabilities["cap_full_cost"] = {"class": "embedding"}
    db.providers["prov_runpod_queue"] = {
        "name": "RunPod queue test",
        "provider_type": "serverless_queue",
    }
    db.leases.append({"id": "lease_1", "provider_id": "prov_runpod_queue", "state": "active"})
    pool = _FakePool(db)

    capability = _capability()
    budget_gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("1.000000"),
        per_request_max_usd=Decimal("0.500000"),
        workload_id_factory=lambda: "wkl_full_cost_1",
    )

    async def fake_runpod_submit() -> dict[str, str]:
        return {"id": "rp_job_full_cost_1", "status": "IN_QUEUE"}

    admitted = await gate_sync_inference(
        capability=capability,
        provider_id="prov_runpod_queue",
        provider_cost={"per_request": "0.100000"},
        payload={"input": "cost path"},
        budget_gate=budget_gate,
        runpod_caller=fake_runpod_submit,
        submitted_at=_NOW,
    )

    workload = db.workloads[admitted.workload_id]
    workload["runpod_job_id"] = admitted.runpod_result["id"]
    assert db.advisory_locks == [PITWALL_BUDGET_LOCK_KEY]
    assert workload["cost_estimate_usd"] == Decimal("0.100000")
    assert workload["state"] == "queued"

    billing = RunPodBillingFake()
    billing.set(
        "rp_job_full_cost_1",
        status="COMPLETED",
        cost_per_hr=Decimal("0.60"),
        worker_time_ms=3_600_000,
        completed_at=_NOW,
    )

    active = await fetch_active_workloads(pool)
    assert active == [{"id": "wkl_full_cost_1", "runpod_job_id": "rp_job_full_cost_1"}]

    billing_data = billing.get(active[0]["runpod_job_id"])
    assert billing_data is not None
    terminal = map_runpod_status(
        billing_data.status,
        cost_per_hr=billing_data.cost_per_hr,
        worker_time_ms=billing_data.worker_time_ms,
        completed_at=billing_data.completed_at,
    )
    assert terminal.terminal is True
    assert terminal.state is not None
    assert terminal.actual_cost == Decimal("0.600000")
    assert terminal.completed_at is not None

    await apply_terminal_state(
        pool,
        workload_id=active[0]["id"],
        state=terminal.state,
        actual_cost=terminal.actual_cost,
        completed_at=terminal.completed_at,
    )
    assert workload["state"] == "completed"
    assert workload["cost_actual_usd"] == Decimal("0.600000")

    await aggregate_daily_cost(pool)
    assert db.cost_daily == {
        (dt.date(2026, 5, 28), "embedding", "serverless_queue"): {
            "workload_count": 1,
            "cost_usd": Decimal("0.600000"),
        }
    }

    from pitwall import cost_exporter

    exporter_app = SimpleNamespace(state=SimpleNamespace(pool=pool, budget=1.0))
    await cost_exporter._refresh(exporter_app)
    metrics = generate_latest().decode()
    assert "pitwall_cloud_spend_month_usd 0.6" in metrics
    assert "pitwall_cloud_budget_pct 60.0" in metrics
    assert 'pitwall_active_workers{provider="RunPod queue test"} 1.0' in metrics
    assert 'pitwall_provider_spend_month_usd{provider="RunPod queue test"} 0.6' in metrics
    assert "pitwall_workload_queue_depth 0.0" in metrics

    sent: list[int] = []

    def fake_send_threshold_email(crossing: Any) -> NotificationResult:
        sent.append(crossing.threshold_pct)
        return NotificationResult(
            threshold_pct=crossing.threshold_pct,
            email_id="email_full_cost",
            error=None,
        )

    monkeypatch.setattr(
        "pitwall.cost.notifications.send_threshold_email",
        fake_send_threshold_email,
    )

    crossings = await evaluate_crossings(
        pool,
        budget_usd=1.0,
        thresholds=(50, 75, 90),
        now=_NOW,
    )
    assert [crossing.threshold_pct for crossing in crossings] == [50]

    notification_results = await send_crossing_notifications(crossings)
    assert sent == [50]
    assert notification_results == [
        NotificationResult(threshold_pct=50, email_id="email_full_cost", error=None)
    ]

    await record_crossings(pool, crossings, now=_NOW)
    assert db.alert_events == {("2026-05", 50): _NOW}

    repeated = await evaluate_crossings(
        pool,
        budget_usd=1.0,
        thresholds=(50, 75, 90),
        now=_NOW,
    )
    assert repeated == []


def _capability() -> Capability:
    return Capability(
        id="cap_full_cost",
        name="full-cost-test",
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode="per_request",
        defaults={"execution_timeout_ms": 60_000},
        created_at=_NOW,
        updated_at=_NOW,
    )


@dataclass
class _FakeCostDatabase:
    capabilities: dict[str, dict[str, Any]]
    providers: dict[str, dict[str, Any]]
    workloads: dict[str, dict[str, Any]]
    leases: list[dict[str, Any]]
    cost_daily: dict[tuple[dt.date, str, str], dict[str, Any]]
    alert_events: dict[tuple[str, int], dt.datetime]
    advisory_locks: list[int]
    kill_count_7d: int

    def __init__(self) -> None:
        self.capabilities = {}
        self.providers = {}
        self.workloads = {}
        self.leases = []
        self.cost_daily = {}
        self.alert_events = {}
        self.advisory_locks = []
        self.kill_count_7d = 0


class _FakePool:
    def __init__(self, db: _FakeCostDatabase) -> None:
        self.db = db

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(_FakeConnection(self.db))


class _AcquireContext:
    def __init__(self, conn: _FakeConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self.conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _TransactionContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeConnection:
    def __init__(self, db: _FakeCostDatabase) -> None:
        self.db = db

    def transaction(self) -> _TransactionContext:
        return _TransactionContext()

    async def execute(self, sql: str, *args: object) -> str:
        if "pg_advisory_xact_lock" in sql:
            self.db.advisory_locks.append(_int_arg(args[0]))
            return "SELECT 1"

        if "SET state = 'running'" in sql:
            workload_id, started_at, input_bytes, input_payload, fallback_chain = args
            workload = self.db.workloads[str(workload_id)]
            workload["state"] = "running"
            workload["started_at"] = _datetime_arg(started_at)
            workload["input_bytes"] = _int_arg(input_bytes)
            workload["input"] = input_payload
            workload["fallback_chain"] = fallback_chain
            return "UPDATE 1"

        if "SET state = $2" in sql and "runpod_job_id" in sql and "completed_at" not in sql:
            workload_id, state, runpod_job_id, output_bytes, result_payload = args
            workload = self.db.workloads[str(workload_id)]
            workload["state"] = _str_arg(state)
            workload["runpod_job_id"] = None if runpod_job_id is None else _str_arg(runpod_job_id)
            workload["output_bytes"] = _int_arg(output_bytes)
            workload["result"] = result_payload
            return "UPDATE 1"

        if "SET state = $2" in sql and "completed_at" in sql:
            (
                workload_id,
                state,
                completed_at,
                execution_ms,
                output_bytes,
                result_payload,
                runpod_job_id,
                error_payload,
            ) = args
            workload = self.db.workloads[str(workload_id)]
            workload["state"] = _str_arg(state)
            workload["completed_at"] = _datetime_arg(completed_at)
            workload["execution_ms"] = _int_arg(execution_ms)
            workload["output_bytes"] = _int_arg(output_bytes)
            workload["result"] = result_payload
            workload["runpod_job_id"] = None if runpod_job_id is None else _str_arg(runpod_job_id)
            workload["error"] = error_payload
            return "UPDATE 1"

        if "UPDATE pitwall.workloads" in sql:
            state, actual_cost, completed_at, workload_id = args
            workload = self.db.workloads[str(workload_id)]
            workload["state"] = _str_arg(state)
            workload["cost_actual_usd"] = _decimal_arg(actual_cost)
            workload["completed_at"] = completed_at
            return "UPDATE 1"

        if "INSERT INTO pitwall.cost_daily" in sql:
            self._aggregate_daily()
            return "INSERT 0 1"

        if "INSERT INTO pitwall.alert_events" in sql:
            month, threshold_pct, sent_at = args
            key = (_str_arg(month), _int_arg(threshold_pct))
            self.db.alert_events.setdefault(key, _datetime_arg(sent_at))
            return "INSERT 0 1"

        raise AssertionError(f"unexpected execute SQL: {sql}")

    async def fetchrow(self, sql: str, *args: object) -> Mapping[str, Any]:
        if "FROM pitwall.webhook_delivery_failures" in sql:
            return {"retries_due": 0, "terminal_failures_24h": 0}
        if "FROM pitwall.retention_runs" in sql:
            return {}
        if "SUM(COALESCE(cost_actual_usd, cost_estimate_usd))" in sql:
            return {"s": self._admission_month_to_date()}
        if "SUM(cost_estimate_usd)" in sql:
            return {"s": self._estimated_month_to_date()}
        if "SUM(cost_actual_usd)" in sql:
            now = _datetime_arg(args[0]) if args else _NOW
            return {"total": self._actual_month_to_date(now)}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql: str, *args: object) -> Any:
        if "SELECT id FROM pitwall.workloads WHERE idempotency_key" in sql:
            return self._workload_id_for_idempotency_key(args[0])

        if "INSERT INTO pitwall.workloads" in sql:
            workload = self._insert_workload(sql, args)
            return workload["id"]

        if "SUM(cost_actual_usd)" in sql:
            return self._actual_month_to_date(_NOW)

        if "FROM pitwall.kill_log" in sql:
            return self.db.kill_count_7d

        if "FROM pitwall.providers WHERE health_status = 'unhealthy'" in sql:
            return sum(
                1
                for provider in self.db.providers.values()
                if provider.get("health_status") == "unhealthy"
            )

        if "WHERE state = 'queued'" in sql:
            return sum(
                1 for workload in self.db.workloads.values() if workload["state"] == "queued"
            )

        if "MIN(submitted_at)" in sql:
            return 0

        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    async def fetch(self, sql: str, *args: object) -> list[Mapping[str, Any]]:
        if "FROM pitwall.workloads" in sql and "runpod_job_id IS NOT NULL" in sql:
            return [
                {"id": workload["id"], "runpod_job_id": workload["runpod_job_id"]}
                for workload in self.db.workloads.values()
                if workload["state"] in {"queued", "running"}
                and workload.get("runpod_job_id") is not None
            ]

        if "FROM pitwall.leases" in sql:
            counts: dict[str, int] = {}
            for lease in self.db.leases:
                if lease["state"] != "active":
                    continue
                provider = self.db.providers[lease["provider_id"]]
                counts[provider["name"]] = counts.get(provider["name"], 0) + 1
            return [
                {"provider": provider_name, "cnt": count}
                for provider_name, count in sorted(counts.items())
            ]

        if "SUM(w.cost_actual_usd)" in sql:
            totals: dict[str, Decimal] = {}
            for workload in self.db.workloads.values():
                provider = self.db.providers[workload["provider_id"]]["name"]
                totals[provider] = totals.get(provider, Decimal("0")) + Decimal(
                    workload.get("cost_actual_usd") or 0
                )
            return [
                {"provider": provider, "spend": spend} for provider, spend in sorted(totals.items())
            ]

        if "FROM pitwall.alert_events" in sql:
            month = _str_arg(args[0])
            return [
                {"threshold_pct": threshold_pct}
                for event_month, threshold_pct in sorted(self.db.alert_events)
                if event_month == month
            ]

        if "UPDATE pitwall.workloads" in sql and "RETURNING id" in sql:
            # _APPLY_TERMINAL_SQL: $1=state $2=cost_actual $3=completed_at $4=id.
            # Mirror the real WHERE clause: only "update" if not already terminal.
            terminal_states = {"completed", "failed", "cancelled", "timed_out"}
            workload_id = _str_arg(args[3])
            wkl = self.db.workloads.get(workload_id)
            if wkl is None or wkl["state"] in terminal_states:
                return []
            wkl["state"] = _str_arg(args[0])
            wkl["cost_actual_usd"] = args[1]
            wkl["completed_at"] = args[2]
            return [{"id": wkl["id"]}]

        raise AssertionError(f"unexpected fetch SQL: {sql}")

    def _insert_workload(self, sql: str, args: tuple[object, ...]) -> dict[str, Any]:
        workload = {
            "id": _str_arg(args[0]),
            "capability_id": _str_arg(args[1]),
            "provider_id": _str_arg(args[2]),
            "type": _str_arg(args[3]),
            "state": "queued",
            "cost_estimate_usd": _decimal_arg(args[4]),
            "submitted_at": _datetime_arg(args[5]),
            "cost_actual_usd": None,
            "completed_at": None,
            "runpod_job_id": None,
            "idempotency_key": _str_arg(args[6]) if "idempotency_key" in sql else None,
        }
        self.db.workloads[workload["id"]] = workload
        return workload

    def _workload_id_for_idempotency_key(self, idempotency_key: object) -> str | None:
        for workload in self.db.workloads.values():
            if workload.get("idempotency_key") == idempotency_key:
                return str(workload["id"])
        return None

    def _estimated_month_to_date(self) -> Decimal:
        return sum(
            (
                _decimal_arg(workload["cost_estimate_usd"])
                for workload in self.db.workloads.values()
                if workload["state"] in {"queued", "running", "completed"}
            ),
            Decimal("0"),
        )

    def _admission_month_to_date(self) -> Decimal:
        month_key = (_NOW.year, _NOW.month)
        return sum(
            (
                _decimal_arg(
                    workload["cost_actual_usd"]
                    if workload["cost_actual_usd"] is not None
                    else workload["cost_estimate_usd"]
                )
                for workload in self.db.workloads.values()
                if _same_month(_datetime_arg(workload["submitted_at"]), month_key)
            ),
            Decimal("0"),
        )

    def _actual_month_to_date(self, now: dt.datetime) -> Decimal:
        month_key = (now.year, now.month)
        return sum(
            (
                _decimal_arg(workload["cost_actual_usd"])
                for workload in self.db.workloads.values()
                if workload["state"] in {"queued", "running", "completed"}
                and workload["cost_actual_usd"] is not None
                and _same_month(_datetime_arg(workload["submitted_at"]), month_key)
            ),
            Decimal("0"),
        )

    def _aggregate_daily(self) -> None:
        rows: dict[tuple[dt.date, str, str], dict[str, Any]] = {}
        for workload in self.db.workloads.values():
            if workload["state"] not in {"completed", "failed", "cancelled", "timed_out"}:
                continue
            capability = self.db.capabilities[workload["capability_id"]]
            provider = self.db.providers[workload["provider_id"]]
            key = (
                _datetime_arg(workload["submitted_at"]).date(),
                _str_arg(capability["class"]),
                _str_arg(provider["provider_type"]),
            )
            row = rows.setdefault(
                key,
                {"workload_count": 0, "cost_usd": Decimal("0")},
            )
            row["workload_count"] += 1
            row["cost_usd"] += _decimal_arg(workload["cost_actual_usd"] or Decimal("0"))
        self.db.cost_daily.update(rows)


def _same_month(value: dt.datetime, month_key: tuple[int, int]) -> bool:
    return (value.year, value.month) == month_key


def _decimal_arg(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        raise TypeError("expected decimal value, got None")
    return Decimal(str(value))


def _datetime_arg(value: object) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value
    raise TypeError(f"expected datetime, got {value!r}")


def _int_arg(value: object) -> int:
    if isinstance(value, int):
        return value
    return int(str(value))


def _str_arg(value: object) -> str:
    if isinstance(value, str):
        return value
    return str(value)
