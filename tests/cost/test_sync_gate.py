from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import pitwall.cost.sync_gate as sync_gate
from pitwall.core import Capability
from pitwall.core.idempotency import IdempotencyReservation
from pitwall.cost.budget_gate import BudgetGate, BudgetRejected, BudgetSnapshot
from pitwall.cost.sync_gate import (
    SyncInferenceRejected,
    SyncInferenceResult,
    estimate_cost,
    gate_sync_inference,
)

pytestmark = pytest.mark.anyio


def _capability(
    cost_mode: str = "per_request",
    execution_timeout_ms: int = 60_000,
) -> Capability:
    return Capability(
        id="cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
        name="embedding.bge-m3",
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode=cost_mode,
        defaults={"execution_timeout_ms": execution_timeout_ms},
        created_at="2026-05-26T14:00:00Z",
        updated_at="2026-05-26T14:00:00Z",
    )


def _mock_pool(current_spend: Decimal, admitted_id: str = "wkl_test") -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="SELECT 1")
    conn.fetchrow = AsyncMock(return_value={"s": current_spend})
    conn.fetchval = AsyncMock(return_value=admitted_id)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acq)
    pool.conn = conn
    return pool


def _recording_pool(fetchrow_result: dict[str, Any] | None = None) -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acq)
    pool.conn = conn
    return pool


class _ReplayTx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _ReplayAcquire:
    def __init__(self, conn: _ReplayConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _ReplayConn:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _ReplayPool:
    def __init__(self) -> None:
        self.workloads: dict[str, dict[str, Any]] = {}
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.conn = _ReplayConn(self)

    def acquire(self) -> _ReplayAcquire:
        return _ReplayAcquire(self.conn)

    def workload_id_for_key(self, idempotency_key: object) -> str | None:
        for workload in self.workloads.values():
            if workload.get("idempotency_key") == idempotency_key:
                return str(workload["id"])
        return None


class _ReplayConn:
    def __init__(self, pool: _ReplayPool) -> None:
        self._pool = pool

    def transaction(self) -> _ReplayTx:
        return _ReplayTx()

    async def execute(self, sql: str, *args: object) -> str:
        self._pool.execute_calls.append((sql, args))
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        if "SET state = 'running'" in sql:
            workload = self._pool.workloads[str(args[0])]
            workload["state"] = "running"
            workload["started_at"] = args[1]
            workload["input_bytes"] = args[2]
            workload["input"] = args[3]
            workload["fallback_chain"] = args[4]
            return "UPDATE 1"
        if "completed_at = $3" in sql:
            workload = self._pool.workloads[str(args[0])]
            workload["state"] = str(args[1])
            workload["completed_at"] = args[2]
            workload["execution_ms"] = args[3]
            workload["output_bytes"] = args[4]
            workload["result"] = args[5]
            workload["runpod_job_id"] = args[6]
            workload["error"] = args[7]
            return "UPDATE 1"
        if "runpod_job_id = COALESCE($3::text" in sql:
            workload = self._pool.workloads[str(args[0])]
            workload["state"] = str(args[1])
            workload["runpod_job_id"] = args[2]
            workload["output_bytes"] = args[3]
            workload["result"] = args[4]
            return "UPDATE 1"
        if "SET state = 'failed'" in sql:
            workload = self._pool.workloads[str(args[0])]
            workload["state"] = "failed"
            workload["completed_at"] = args[1]
            workload["execution_ms"] = args[2]
            workload["error"] = args[3]
            return "UPDATE 1"
        if "SET fallback_chain = $2::text[]" in sql:
            workload = self._pool.workloads[str(args[0])]
            workload["fallback_chain"] = args[1]
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        if "SUM(" in sql and "FROM pitwall.workloads" in sql:
            return {"s": Decimal("0")}
        if "FROM pitwall.workloads" in sql and "WHERE id = $1" in sql:
            workload = self._pool.workloads.get(str(args[0]))
            if workload is None:
                return None
            return {
                "state": workload["state"],
                "result": workload.get("result"),
            }
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql: str, *args: object) -> Any:
        if "SELECT id FROM pitwall.workloads WHERE idempotency_key = $1" in sql:
            return self._pool.workload_id_for_key(args[0])
        if "INSERT INTO pitwall.workloads" in sql:
            workload = {
                "id": str(args[0]),
                "state": "queued",
                "cost_estimate_usd": args[4],
                "input": None,
                "result": None,
                "idempotency_key": str(args[6]) if "idempotency_key" in sql else None,
            }
            self._pool.workloads[workload["id"]] = workload
            return workload["id"]
        raise AssertionError(f"unexpected fetchval SQL: {sql}")


def test_sync_inference_rejected_preserves_reason_and_budget_error() -> None:
    budget_error = BudgetRejected(
        "monthly_budget",
        BudgetSnapshot(
            monthly_budget_usd=Decimal("10.000000"),
            per_request_max_usd=Decimal("2.000000"),
            mtd_spend_usd=Decimal("9.500000"),
            estimate_usd=Decimal("0.750000"),
            budget_remaining_usd=Decimal("0.500000"),
        ),
    )

    exc = SyncInferenceRejected("monthly_budget", budget_error)

    assert str(exc) == "monthly_budget"
    assert exc.budget_error is budget_error


def test_idempotency_body_hash_uses_canonical_json() -> None:
    payload = {"z": [3, None], "a": {"b": True}}
    expected_payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert sync_gate._idempotency_body_hash(None) == hashlib.sha256(b"null").hexdigest()
    assert sync_gate._idempotency_body_hash(payload) == expected_payload_hash
    assert sync_gate._idempotency_body_hash({"a": {"b": True}, "z": [3, None]}) == (
        expected_payload_hash
    )


async def test_persistence_helpers_emit_exact_sql_arguments() -> None:
    pool = _recording_pool()
    started_at = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)
    completed_at = dt.datetime(2026, 5, 28, 12, 0, 2, tzinfo=dt.UTC)

    await sync_gate._mark_workload_running(
        pool,
        workload_id="wkl_helpers",
        started_at=started_at,
        input_bytes=17,
        input_payload={"prompt": "hi"},
        fallback_chain=["prov_a", "prov_b"],
    )
    pool.conn.execute.assert_awaited_once_with(
        sync_gate._MARK_WORKLOAD_RUNNING_SQL,
        "wkl_helpers",
        started_at,
        17,
        {"prompt": "hi"},
        ["prov_a", "prov_b"],
    )

    pool.conn.execute.reset_mock()
    await sync_gate._mark_workload_terminal(
        pool,
        workload_id="wkl_helpers",
        state="failed",
        completed_at=completed_at,
        execution_ms=2000,
        output_bytes=31,
        result_payload={"status": "FAILED"},
        runpod_job_id="job-1",
        error={"message": "failed"},
    )
    pool.conn.execute.assert_awaited_once_with(
        sync_gate._MARK_WORKLOAD_TERMINAL_SQL,
        "wkl_helpers",
        "failed",
        completed_at,
        2000,
        31,
        {"status": "FAILED"},
        "job-1",
        {"message": "failed"},
    )

    pool.conn.execute.reset_mock()
    await sync_gate._mark_workload_active_after_call(
        pool,
        workload_id="wkl_helpers",
        state="running",
        runpod_job_id="job-active",
        output_bytes=29,
        result_payload={"status": "IN_PROGRESS"},
    )
    pool.conn.execute.assert_awaited_once_with(
        sync_gate._MARK_WORKLOAD_ACTIVE_AFTER_CALL_SQL,
        "wkl_helpers",
        "running",
        "job-active",
        29,
        {"status": "IN_PROGRESS"},
    )

    pool.conn.execute.reset_mock()
    await sync_gate._mark_workload_failed(
        pool,
        workload_id="wkl_helpers",
        completed_at=completed_at,
        execution_ms=2000,
        error={"type": "RuntimeError", "message": "boom"},
    )
    pool.conn.execute.assert_awaited_once_with(
        sync_gate._MARK_WORKLOAD_FAILED_SQL,
        "wkl_helpers",
        completed_at,
        2000,
        {"type": "RuntimeError", "message": "boom"},
    )

    pool.conn.execute.reset_mock()
    await sync_gate.update_workload_fallback_chain(pool, "wkl_helpers", ["prov_b"])
    pool.conn.execute.assert_awaited_once_with(
        sync_gate._UPDATE_WORKLOAD_FALLBACK_CHAIN_SQL,
        "wkl_helpers",
        ["prov_b"],
    )


async def test_load_workload_replay_result_preserves_mapping_result() -> None:
    pool = _recording_pool({"state": "completed", "result": {"answer": 42}})

    result = await sync_gate._load_workload_replay_result(pool, workload_id="wkl_replay")

    assert result == {"answer": 42}
    pool.conn.fetchrow.assert_awaited_once_with(
        sync_gate._LOAD_WORKLOAD_REPLAY_SQL,
        "wkl_replay",
    )


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"state": "running", "result": None}, {"status": "running"}),
        ({"state": "completed", "result": "raw text"}, {"result": "raw text"}),
    ],
)
async def test_load_workload_replay_result_wraps_non_mapping_results(
    row: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    pool = _recording_pool(row)

    result = await sync_gate._load_workload_replay_result(pool, workload_id="wkl_replay")

    assert result == expected


async def test_load_workload_replay_result_rejects_missing_workload() -> None:
    pool = _recording_pool(None)

    with pytest.raises(
        RuntimeError, match="idempotency replay workload 'wkl_missing' was not found"
    ):
        await sync_gate._load_workload_replay_result(pool, workload_id="wkl_missing")


def test_json_helpers_make_payloads_database_safe_and_stable() -> None:
    moment = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)

    class Dumpable:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"when": moment, "amount": Decimal("1.25")}

    safe = sync_gate._json_safe(
        {
            7: (Decimal("1.25"), b"caf\xc3\xa9"),
            "finite": 1.5,
            "nan": float("nan"),
            "moment": moment,
            "model": Dumpable(),
            "none": None,
        }
    )

    assert safe == {
        "7": ["1.25", "café"],
        "finite": 1.5,
        "nan": "nan",
        "moment": "2026-05-28T12:00:00+00:00",
        "model": {"when": "2026-05-28T12:00:00+00:00", "amount": "1.25"},
        "none": None,
    }
    assert sync_gate._json_object({"already": "object"}, wrapper_key="payload") == {
        "already": "object"
    }
    assert sync_gate._json_object(["not", "object"], wrapper_key="payload") == {
        "payload": ["not", "object"]
    }
    assert sync_gate._json_bytes({"b": "é", "a": 1}) == len('{"a":1,"b":"é"}'.encode())


@pytest.mark.parametrize("key", ["id", "job_id", "jobId", "runpod_job_id"])
def test_extract_runpod_job_id_from_top_level_keys(key: str) -> None:
    assert sync_gate._extract_runpod_job_id({key: 123}) == "123"


@pytest.mark.parametrize("key", ["id", "job_id", "jobId", "runpod_job_id"])
def test_extract_runpod_job_id_from_raw_payload_keys(key: str) -> None:
    assert sync_gate._extract_runpod_job_id({"raw": {key: "job-raw"}}) == "job-raw"


def test_extract_runpod_job_id_returns_none_when_absent() -> None:
    assert sync_gate._extract_runpod_job_id({"raw": "not a mapping"}) is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("IN_QUEUE", "queued"),
        ("IN_PROGRESS", "running"),
        ("COMPLETED", None),
        (None, None),
    ],
)
def test_active_state_from_runpod_result(status: str | None, expected: str | None) -> None:
    payload = {} if status is None else {"status": status}
    assert sync_gate._active_state_from_runpod_result(payload) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("FAILED", "failed"),
        ("CANCELLED", "cancelled"),
        ("TIMED_OUT", "timed_out"),
        ("TIMEOUT", "timed_out"),
        ("TIME_OUT", "timed_out"),
        ("COMPLETED", "completed"),
        (None, "completed"),
    ],
)
def test_terminal_state_from_runpod_result(status: str | None, expected: str) -> None:
    payload = {} if status is None else {"status": status}
    assert sync_gate._terminal_state_from_runpod_result(payload) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"error": {"code": "bad_request"}}, {"code": "bad_request"}),
        ({"error": "plain failure"}, {"message": "plain failure"}),
        ({"status": "FAILED"}, {"status": "FAILED"}),
        ({}, {"status": "unknown"}),
    ],
)
def test_result_error_payload_shapes_provider_failures(
    payload: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    assert sync_gate._result_error_payload(payload) == expected


def test_error_payload_preserves_exception_type_and_message() -> None:
    exc = RuntimeError("RunPod timeout")

    assert sync_gate._error_payload(exc) == {
        "type": "RuntimeError",
        "message": "RunPod timeout",
    }


def test_utc_now_returns_timezone_aware_utc_datetime() -> None:
    now = sync_gate._utc_now()

    assert now.tzinfo is dt.UTC


def test_elapsed_ms_uses_milliseconds_and_clamps_negative_values() -> None:
    started_at = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)
    completed_at = dt.datetime(2026, 5, 28, 12, 0, 2, 250000, tzinfo=dt.UTC)

    assert sync_gate._elapsed_ms(started_at, completed_at) == 2250
    assert sync_gate._elapsed_ms(completed_at, started_at) == 0


async def test_gate_sync_inference_admits_then_calls_runpod() -> None:
    pool = _mock_pool(current_spend=Decimal("1.000000"), admitted_id="wkl_admitted_1")
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_admitted_1",
    )
    runpod_call_count = 0

    async def fake_runpod_call() -> dict[str, Any]:
        nonlocal runpod_call_count
        runpod_call_count += 1
        return {"id": "job-1", "status": "COMPLETED", "output": {"result": "ok"}}

    result = await gate_sync_inference(
        capability=_capability(cost_mode="per_request"),
        provider_id="prov_runpod_bge",
        provider_cost={"per_request": "0.50"},
        payload={},
        budget_gate=gate,
        runpod_caller=fake_runpod_call,
    )

    assert isinstance(result, SyncInferenceResult)
    assert result.workload_id == "wkl_admitted_1"
    assert result.runpod_result["status"] == "COMPLETED"
    assert runpod_call_count == 1


async def test_gate_sync_inference_raises_budget_rejected_without_calling_runpod() -> None:
    pool = _mock_pool(current_spend=Decimal("9.500000"))
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
    )
    runpod_call_count = 0

    async def fake_runpod_call() -> dict[str, Any]:
        nonlocal runpod_call_count
        runpod_call_count += 1
        return {"should not": "be called"}

    with pytest.raises(BudgetRejected) as exc_info:
        await gate_sync_inference(
            capability=_capability(cost_mode="per_request"),
            provider_id="prov_runpod_bge",
            provider_cost={"per_request": "0.75"},
            payload={},
            budget_gate=gate,
            runpod_caller=fake_runpod_call,
        )

    assert exc_info.value.reason == "monthly_budget"
    assert runpod_call_count == 0


async def test_gate_sync_inference_rejects_per_request_cap_before_runpod() -> None:
    pool = _mock_pool(current_spend=Decimal("0"))
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("0.01"),
    )
    runpod_called = False

    async def fake_runpod_call() -> dict[str, Any]:
        nonlocal runpod_called
        runpod_called = True
        return {}

    with pytest.raises(BudgetRejected) as exc_info:
        await gate_sync_inference(
            capability=_capability(cost_mode="per_request"),
            provider_id="prov_runpod_bge",
            provider_cost={"per_request": "5.00"},
            payload={},
            budget_gate=gate,
            runpod_caller=fake_runpod_call,
        )

    assert exc_info.value.reason == "per_request_cap"
    assert not runpod_called


async def test_gate_sync_inference_uses_per_second_estimator() -> None:
    pool = _mock_pool(current_spend=Decimal("0"), admitted_id="wkl_ps_1")
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("100.000000"),
        per_request_max_usd=Decimal("10.000000"),
        workload_id_factory=lambda: "wkl_ps_1",
    )

    async def fake_runpod_call() -> dict[str, Any]:
        return {"status": "ok"}

    cap = _capability(cost_mode="per_second", execution_timeout_ms=30_000)
    result = await gate_sync_inference(
        capability=cap,
        provider_id="prov_pod",
        provider_cost={"per_second_active": "0.001"},
        payload={},
        budget_gate=gate,
        runpod_caller=fake_runpod_call,
    )

    assert result.workload_id == "wkl_ps_1"

    insert_args = pool.conn.fetchval.await_args.args
    assert insert_args[5] == Decimal("0.030000")


async def test_gate_sync_inference_uses_per_token_estimator() -> None:
    pool = _mock_pool(current_spend=Decimal("0"), admitted_id="wkl_tok_1")
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("100.000000"),
        per_request_max_usd=Decimal("10.000000"),
        workload_id_factory=lambda: "wkl_tok_1",
    )

    async def fake_runpod_call() -> dict[str, Any]:
        return {"status": "ok"}

    cap = _capability(cost_mode="per_token")
    result = await gate_sync_inference(
        capability=cap,
        provider_id="prov_openai",
        provider_cost={
            "per_million_input_tokens": "0.10",
            "per_million_output_tokens": "0.40",
        },
        payload={"input_tokens": 1000, "max_tokens": 256},
        budget_gate=gate,
        runpod_caller=fake_runpod_call,
    )

    assert result.workload_id == "wkl_tok_1"

    insert_args = pool.conn.fetchval.await_args.args
    estimate = insert_args[5]
    expected = (Decimal("0.10") * Decimal(1000) + Decimal("0.40") * Decimal(256)) / Decimal(
        1_000_000
    )
    from decimal import ROUND_HALF_UP

    expected_quantized = expected.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    assert estimate == expected_quantized


async def test_gate_sync_inference_replays_existing_idempotency_result_without_second_runpod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _ReplayPool()
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_replay_1",
    )

    async def fake_reserve_idempotency_key(*_args: Any, **kwargs: Any) -> IdempotencyReservation:
        return IdempotencyReservation(is_new=True, workload_id=str(kwargs["workload_id"]))

    monkeypatch.setattr(
        "pitwall.cost.sync_gate.reserve_idempotency_key",
        fake_reserve_idempotency_key,
    )

    runpod_call_count = 0

    async def fake_runpod_call() -> dict[str, Any]:
        nonlocal runpod_call_count
        runpod_call_count += 1
        return {
            "id": f"job-{runpod_call_count}",
            "status": "COMPLETED",
            "output": {"call": runpod_call_count},
        }

    kwargs = {
        "capability": _capability(cost_mode="per_request"),
        "provider_id": "prov_runpod_bge",
        "provider_cost": {"per_request": "0.50"},
        "payload": {"prompt": "same request"},
        "budget_gate": gate,
        "runpod_caller": fake_runpod_call,
        "idempotency_key": "idem-sync-replay",
    }

    first = await gate_sync_inference(**kwargs)
    second = await gate_sync_inference(**kwargs)

    assert runpod_call_count == 1
    assert first.workload_id == second.workload_id == "wkl_replay_1"
    assert second.runpod_result == first.runpod_result


async def test_gate_sync_inference_reserves_idempotency_key_with_body_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _ReplayPool()
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_idem_hash",
    )
    captured: dict[str, Any] = {}

    async def fake_reserve_idempotency_key(conn: object, **kwargs: Any) -> IdempotencyReservation:
        captured["conn"] = conn
        captured.update(kwargs)
        return IdempotencyReservation(is_new=True, workload_id=str(kwargs["workload_id"]))

    monkeypatch.setattr(
        "pitwall.cost.sync_gate.reserve_idempotency_key",
        fake_reserve_idempotency_key,
    )

    async def fake_runpod_call() -> dict[str, Any]:
        return {"id": "job-idem", "status": "COMPLETED"}

    payload = {"z": [3, None], "a": {"b": True}}

    result = await gate_sync_inference(
        capability=_capability(cost_mode="per_request"),
        provider_id="prov_runpod_bge",
        provider_cost={"per_request": "0.50"},
        payload=payload,
        budget_gate=gate,
        runpod_caller=fake_runpod_call,
        idempotency_key="idem-hash-1234567890",
    )

    assert result.workload_id == "wkl_idem_hash"
    assert captured["conn"] is pool.conn
    assert {key: value for key, value in captured.items() if key != "conn"} == {
        "key": "idem-hash-1234567890",
        "body_hash": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "workload_id": "wkl_pending_idem-hash-123456",
    }
    assert pool.workloads["wkl_idem_hash"]["idempotency_key"] == "idem-hash-1234567890"


async def test_gate_sync_inference_persists_input_and_terminal_result_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _ReplayPool()
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_persisted",
    )
    started_at = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)
    completed_at = dt.datetime(2026, 5, 28, 12, 0, 2, 250000, tzinfo=dt.UTC)
    moments = iter([started_at, completed_at])
    monkeypatch.setattr("pitwall.cost.sync_gate._utc_now", lambda: next(moments))

    async def fake_runpod_call() -> dict[str, Any]:
        return {
            "job_id": 987,
            "status": "COMPLETED",
            "output": {"score": Decimal("1.25")},
        }

    payload = {"prompt": b"caf\xc3\xa9", "limit": Decimal("1.25")}

    result = await gate_sync_inference(
        capability=_capability(cost_mode="per_request"),
        provider_id="prov_primary",
        provider_cost={"per_request": "0.50"},
        payload=payload,
        budget_gate=gate,
        runpod_caller=fake_runpod_call,
        input_bytes=99,
        fallback_chain=["prov_primary", "prov_backup"],
    )

    workload = pool.workloads[result.workload_id]
    assert workload["state"] == "completed"
    assert workload["started_at"] == started_at
    assert workload["completed_at"] == completed_at
    assert workload["execution_ms"] == 2250
    assert workload["input_bytes"] == 99
    assert workload["input"] == {"prompt": "café", "limit": "1.25"}
    assert workload["fallback_chain"] == ["prov_primary", "prov_backup"]
    assert workload["result"] == {
        "job_id": 987,
        "status": "COMPLETED",
        "output": {"score": "1.25"},
    }
    assert workload["runpod_job_id"] == "987"
    assert workload["output_bytes"] == sync_gate._json_bytes(workload["result"])
    assert workload["error"] is None


async def test_gate_sync_inference_persists_active_runpod_result() -> None:
    pool = _ReplayPool()
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_active",
    )

    async def fake_runpod_call() -> dict[str, Any]:
        return {"id": "job-active", "status": "IN_PROGRESS", "output": {"partial": True}}

    result = await gate_sync_inference(
        capability=_capability(cost_mode="per_request"),
        provider_id="prov_runpod_bge",
        provider_cost={"per_request": "0.50"},
        payload={},
        budget_gate=gate,
        runpod_caller=fake_runpod_call,
    )

    workload = pool.workloads[result.workload_id]
    assert workload["state"] == "running"
    assert workload["runpod_job_id"] == "job-active"
    assert workload["result"] == {
        "id": "job-active",
        "status": "IN_PROGRESS",
        "output": {"partial": True},
    }
    assert workload["output_bytes"] == sync_gate._json_bytes(workload["result"])
    assert "completed_at" not in workload
    assert "error" not in workload


async def test_gate_sync_inference_wraps_non_mapping_runpod_result_for_persistence() -> None:
    pool = _ReplayPool()
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_wrapped_result",
    )

    async def fake_runpod_call() -> list[str]:
        return ["raw-output"]

    result = await gate_sync_inference(
        capability=_capability(cost_mode="per_request"),
        provider_id="prov_runpod_bge",
        provider_cost={"per_request": "0.50"},
        payload={},
        budget_gate=gate,
        runpod_caller=fake_runpod_call,
    )

    workload = pool.workloads[result.workload_id]
    assert result.runpod_result == ["raw-output"]
    assert workload["state"] == "completed"
    assert workload["result"] == {"result": ["raw-output"]}
    assert workload["output_bytes"] == sync_gate._json_bytes(workload["result"])


async def test_gate_sync_inference_persists_provider_failure_as_terminal_error() -> None:
    pool = _ReplayPool()
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_provider_failed",
    )

    async def fake_runpod_call() -> dict[str, Any]:
        return {"runpod_job_id": "job-failed", "status": "FAILED", "error": "provider failed"}

    result = await gate_sync_inference(
        capability=_capability(cost_mode="per_request"),
        provider_id="prov_runpod_bge",
        provider_cost={"per_request": "0.50"},
        payload={},
        budget_gate=gate,
        runpod_caller=fake_runpod_call,
    )

    workload = pool.workloads[result.workload_id]
    assert workload["state"] == "failed"
    assert workload["runpod_job_id"] == "job-failed"
    assert workload["error"] == {"message": "provider failed"}


def test_estimate_cost_per_request() -> None:
    cap = _capability(cost_mode="per_request")

    cost = estimate_cost(
        capability=cap,
        provider_cost={"per_request": "0.00025"},
        payload={},
    )

    assert cost == Decimal("0.000250")


def test_estimate_cost_per_second() -> None:
    cap = _capability(cost_mode="per_second", execution_timeout_ms=120_000)

    cost = estimate_cost(
        capability=cap,
        provider_cost={"per_second_active": "0.002"},
        payload={},
    )

    assert cost == Decimal("0.240000")


def test_estimate_cost_per_token() -> None:
    cap = _capability(cost_mode="per_token")

    cost = estimate_cost(
        capability=cap,
        provider_cost={
            "per_million_input_tokens": "0.10",
            "per_million_output_tokens": "0.40",
        },
        payload={"input_tokens": 500, "max_tokens": 100},
    )

    expected = (Decimal("0.10") * Decimal(500) + Decimal("0.40") * Decimal(100)) / Decimal(
        1_000_000
    )
    from decimal import ROUND_HALF_UP

    expected_quantized = expected.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    assert cost == expected_quantized


async def test_gate_sync_inference_workload_type_is_inference() -> None:
    pool = _mock_pool(current_spend=Decimal("0"), admitted_id="wkl_inf_type")
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_inf_type",
    )

    async def fake_runpod_call() -> dict[str, Any]:
        return {}

    await gate_sync_inference(
        capability=_capability(cost_mode="per_request"),
        provider_id="prov_1",
        provider_cost={"per_request": "0.10"},
        payload={},
        budget_gate=gate,
        runpod_caller=fake_runpod_call,
    )

    insert_args = pool.conn.fetchval.await_args.args
    assert insert_args[4] == "inference"


async def test_gate_sync_inference_propagates_runpod_error_after_admission() -> None:
    pool = _ReplayPool()
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_run_err",
    )

    async def failing_runpod_call() -> dict[str, Any]:
        raise RuntimeError("RunPod timeout")

    with pytest.raises(RuntimeError, match="RunPod timeout"):
        await gate_sync_inference(
            capability=_capability(cost_mode="per_request"),
            provider_id="prov_1",
            provider_cost={"per_request": "0.10"},
            payload={},
            budget_gate=gate,
            runpod_caller=failing_runpod_call,
        )

    workload = pool.workloads["wkl_run_err"]
    assert workload["state"] == "failed"
    assert workload["error"] == {
        "type": "RuntimeError",
        "message": "RunPod timeout",
    }
