from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.core import Capability
from pitwall.cost.budget_gate import (
    PITWALL_BUDGET_LOCK_KEY,
    BudgetAdmission,
    BudgetGate,
    BudgetRejected,
    BudgetSnapshot,
)
from pitwall.cost.estimator import quote_cost


def _capability(
    cost_mode: str = "per_token",
    execution_timeout_ms: int = 60_000,
) -> Capability:
    return Capability(
        id="cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
        name="llm.token-bound",
        version="1.0.0",
        **{"class": "llm"},
        cost_mode=cost_mode,
        defaults={"execution_timeout_ms": execution_timeout_ms},
        created_at="2026-05-26T14:00:00Z",
        updated_at="2026-05-26T14:00:00Z",
    )


def test_pitwall_budget_lock_key_is_derived_from_literal() -> None:
    assert int.from_bytes(b"PITWBUDG", "big") == PITWALL_BUDGET_LOCK_KEY


def test_budget_snapshot_has_json_serializable_dump() -> None:
    snapshot = BudgetSnapshot(
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        mtd_spend_usd=Decimal("9.500000"),
        estimate_usd=Decimal("0.750000"),
        budget_remaining_usd=Decimal("0.500000"),
    )

    assert snapshot.model_dump()["monthly_budget_usd"] == Decimal("10.000000")
    serialized = snapshot.model_dump(mode="json")
    assert serialized == {
        "monthly_budget_usd": "10.000000",
        "per_request_max_usd": "2.000000",
        "mtd_spend_usd": "9.500000",
        "estimate_usd": "0.750000",
        "budget_remaining_usd": "0.500000",
    }
    json.dumps(serialized)
    assert json.loads(snapshot.model_dump_json()) == serialized


def test_budget_rejected_response_body_is_http_402_contract() -> None:
    snapshot = BudgetSnapshot(
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        mtd_spend_usd=Decimal("9.500000"),
        estimate_usd=Decimal("0.750000"),
        budget_remaining_usd=Decimal("0.500000"),
    )

    exc = BudgetRejected("monthly_budget", snapshot)
    body = exc.to_response_body()

    assert str(exc) == "monthly_budget"
    assert exc.status_code == 402
    assert exc.error_code == "budget_rejected"
    assert exc.to_http_response_body() == body
    assert body == {
        "error": "budget_rejected",
        "reason": "monthly_budget",
        "snapshot": {
            "monthly_budget_usd": "10.000000",
            "per_request_max_usd": "2.000000",
            "mtd_spend_usd": "9.500000",
            "estimate_usd": "0.750000",
            "budget_remaining_usd": "0.500000",
        },
    }
    json.dumps(body)


def test_budget_gate_launch_workload_type_defaults_are_inference() -> None:
    assert (
        inspect.signature(BudgetGate.try_launch).parameters["workload_type"].default == "inference"
    )
    assert (
        inspect.signature(BudgetGate.try_launch_admission).parameters["workload_type"].default
        == "inference"
    )


def test_rejects_non_positive_budget_config() -> None:
    with pytest.raises(ValueError, match="monthly_budget_usd.*positive"):
        BudgetGate(
            _mock_pool(current_spend=Decimal("0")), monthly_budget_usd=0, per_request_max_usd=1
        )
    with pytest.raises(ValueError, match="per_request_max_usd.*positive"):
        BudgetGate(
            _mock_pool(current_spend=Decimal("0")), monthly_budget_usd=1, per_request_max_usd=0
        )


def test_budget_gate_loads_budget_config_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PITWALL_MONTHLY_BUDGET_USD", "12.500000")
    monkeypatch.setenv("PITWALL_PER_REQUEST_MAX_USD", "0.750000")

    gate = BudgetGate(_mock_pool(current_spend=Decimal("0")))

    assert gate.monthly_budget_usd == Decimal("12.500000")
    assert gate.per_request_max_usd == Decimal("0.750000")


def test_budget_gate_rejects_missing_environment_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PITWALL_MONTHLY_BUDGET_USD", raising=False)
    monkeypatch.delenv("PITWALL_PER_REQUEST_MAX_USD", raising=False)

    with pytest.raises(ValueError, match="PITWALL_MONTHLY_BUDGET_USD must be set"):
        BudgetGate(_mock_pool(current_spend=Decimal("0")))

    monkeypatch.setenv("PITWALL_MONTHLY_BUDGET_USD", "10.000000")
    with pytest.raises(ValueError, match="PITWALL_PER_REQUEST_MAX_USD must be set"):
        BudgetGate(_mock_pool(current_spend=Decimal("0")))


@pytest.mark.parametrize(
    ("monthly_budget_usd", "per_request_max_usd", "message"),
    [
        (
            "not-decimal",
            Decimal("1.000000"),
            "monthly_budget_usd must be a decimal value",
        ),
        (Decimal("10.000000"), "Infinity", "per_request_max_usd must be finite"),
    ],
)
def test_budget_gate_rejects_invalid_decimal_budget_config(
    monthly_budget_usd: object,
    per_request_max_usd: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        BudgetGate(
            _mock_pool(current_spend=Decimal("0")),
            monthly_budget_usd=monthly_budget_usd,  # type: ignore[arg-type]  # reason: intentionally wrong type to exercise validation
            per_request_max_usd=per_request_max_usd,  # type: ignore[arg-type]  # reason: intentionally wrong type to exercise validation
        )


@pytest.mark.parametrize(
    ("monthly_budget_usd", "per_request_max_usd", "message"),
    [
        (True, Decimal("1.000000"), "monthly_budget_usd must be a decimal value"),
        (Decimal("10.000000"), False, "per_request_max_usd must be a decimal value"),
    ],
)
def test_budget_gate_rejects_bool_budget_config(
    monthly_budget_usd: object,
    per_request_max_usd: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        BudgetGate(
            _mock_pool(current_spend=Decimal("0")),
            monthly_budget_usd=monthly_budget_usd,  # type: ignore[arg-type]  # reason: intentionally wrong type to exercise validation
            per_request_max_usd=per_request_max_usd,  # type: ignore[arg-type]  # reason: intentionally wrong type to exercise validation
        )


@pytest.mark.anyio
async def test_try_launch_inserts_workload_under_advisory_lock() -> None:
    pool = _mock_pool(current_spend=Decimal("1.250000"), admitted_id="wkl_test_001")
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_test_001",
    )
    submitted_at = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)

    workload_id = await gate.try_launch(
        capability_id="cap_embed",
        provider_id="prov_runpod_bge",
        estimate_usd=Decimal("0.750000"),
        submitted_at=submitted_at,
    )

    conn = pool.conn
    assert workload_id == "wkl_test_001"
    conn.execute.assert_awaited_once_with(
        "SELECT pg_advisory_xact_lock($1)",
        PITWALL_BUDGET_LOCK_KEY,
    )

    spend_sql = conn.fetchrow.await_args.args[0]
    assert "FROM pitwall.workloads" in spend_sql
    assert "SUM(COALESCE(cost_actual_usd, cost_estimate_usd))" in spend_sql
    assert "submitted_at >= date_trunc('month', now() AT TIME ZONE 'UTC')" in spend_sql
    assert "state IN ('queued','running','completed')" not in spend_sql

    insert_args = conn.fetchval.await_args.args
    insert_sql = insert_args[0]
    assert "INSERT INTO pitwall.workloads" in insert_sql
    assert "cost_estimate_usd" in insert_sql
    assert "submitted_at" in insert_sql
    assert "'queued'" in insert_sql
    assert "RETURNING id" in insert_sql
    assert insert_args[1:] == (
        "wkl_test_001",
        "cap_embed",
        "prov_runpod_bge",
        "inference",
        Decimal("0.750000"),
        submitted_at,
    )


@pytest.mark.anyio
async def test_try_launch_preserves_custom_workload_type() -> None:
    pool = _mock_pool(current_spend=Decimal("0"), admitted_id="wkl_custom_type")
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_custom_type",
    )

    workload_id = await gate.try_launch(
        capability_id="cap_batch",
        provider_id="prov_batch",
        estimate_usd=Decimal("0.250000"),
        workload_type="batch",
    )

    assert workload_id == "wkl_custom_type"
    insert_args = pool.conn.fetchval.await_args.args
    assert insert_args[4] == "batch"
    assert isinstance(insert_args[6], datetime)
    assert insert_args[6].tzinfo is UTC


@pytest.mark.anyio
async def test_try_launch_passes_default_workload_type_through_wrapper() -> None:
    gate = BudgetGate(
        _mock_pool(current_spend=Decimal("0")),
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
    )
    capture = AsyncMock(return_value=BudgetAdmission(workload_id="wkl_wrapped", is_new=True))
    gate.try_launch_admission = capture  # type: ignore[method-assign]  # reason: test seam: replace bound method with mock

    workload_id = await gate.try_launch(
        capability_id="cap_embed",
        provider_id="prov_runpod_bge",
        estimate_usd=Decimal("0.750000"),
    )

    assert workload_id == "wkl_wrapped"
    assert capture.await_args.kwargs["workload_type"] == "inference"


@pytest.mark.anyio
async def test_try_launch_rejects_monthly_budget_under_lock() -> None:
    pool = _mock_pool(current_spend=Decimal("9.500000"))
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_not_inserted",
    )

    with pytest.raises(BudgetRejected) as exc_info:
        await gate.try_launch(
            capability_id="cap_embed",
            provider_id="prov_runpod_bge",
            estimate_usd=Decimal("0.750000"),
        )

    conn = pool.conn
    assert exc_info.value.reason == "monthly_budget"
    assert isinstance(exc_info.value.snapshot, BudgetSnapshot)
    assert exc_info.value.snapshot.mtd_spend_usd == Decimal("9.500000")
    assert exc_info.value.snapshot.estimate_usd == Decimal("0.750000")
    assert exc_info.value.snapshot.budget_remaining_usd == Decimal("0.500000")
    conn.execute.assert_awaited_once_with(
        "SELECT pg_advisory_xact_lock($1)",
        PITWALL_BUDGET_LOCK_KEY,
    )
    conn.fetchval.assert_not_awaited()


@pytest.mark.anyio
async def test_try_launch_monthly_budget_snapshot_clamps_negative_remaining() -> None:
    pool = _mock_pool(current_spend=Decimal("12.000000"))
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_not_inserted",
    )

    with pytest.raises(BudgetRejected) as exc_info:
        await gate.try_launch(
            capability_id="cap_embed",
            provider_id="prov_runpod_bge",
            estimate_usd=Decimal("0.750000"),
        )

    assert exc_info.value.snapshot.monthly_budget_usd == Decimal("10.000000")
    assert exc_info.value.snapshot.per_request_max_usd == Decimal("2.000000")
    assert exc_info.value.snapshot.mtd_spend_usd == Decimal("12.000000")
    assert exc_info.value.snapshot.estimate_usd == Decimal("0.750000")
    assert exc_info.value.snapshot.budget_remaining_usd == Decimal("0")
    pool.conn.fetchval.assert_not_awaited()


@pytest.mark.anyio
async def test_try_launch_checks_idempotency_after_advisory_lock() -> None:
    pool = _mock_pool_with_idempotency(
        current_spend=Decimal("9.500000"),
        existing_id_for_key="wkl_existing",
    )
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_not_inserted",
    )

    workload_id = await gate.try_launch(
        capability_id="cap_embed",
        provider_id="prov_runpod_bge",
        estimate_usd=Decimal("0.750000"),
        idempotency_key="idem_existing",
    )

    conn = pool.conn
    assert workload_id == "wkl_existing"
    conn.execute.assert_awaited_once_with(
        "SELECT pg_advisory_xact_lock($1)",
        PITWALL_BUDGET_LOCK_KEY,
    )
    method_names = [mock_call[0] for mock_call in conn.method_calls]
    assert method_names.index("execute") < method_names.index("fetchval")
    conn.fetchval.assert_awaited_once_with(
        "SELECT id FROM pitwall.workloads WHERE idempotency_key = $1",
        "idem_existing",
    )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.anyio
async def test_try_launch_admission_reports_existing_idempotency_row() -> None:
    pool = _mock_pool_with_idempotency(
        current_spend=Decimal("9.500000"),
        existing_id_for_key="wkl_existing",
    )
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_not_inserted",
    )

    admission = await gate.try_launch_admission(
        capability_id="cap_embed",
        provider_id="prov_runpod_bge",
        estimate_usd=Decimal("0.750000"),
        idempotency_key="idem_existing",
    )

    assert admission.workload_id == "wkl_existing"
    assert not admission.is_new
    pool.conn.fetchrow.assert_not_awaited()


@pytest.mark.anyio
async def test_try_launch_replay_bypasses_per_request_cap_after_existing_idempotency_hit() -> None:
    pool = _mock_pool_with_idempotency(
        current_spend=Decimal("9.500000"),
        existing_id_for_key="wkl_existing",
    )
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
        workload_id_factory=lambda: "wkl_not_inserted",
    )

    admission = await gate.try_launch_admission(
        capability_id="cap_embed",
        provider_id="prov_runpod_bge",
        estimate_usd=Decimal("2.500000"),
        idempotency_key="idem_existing",
    )

    assert admission == BudgetAdmission(workload_id="wkl_existing", is_new=False)
    conn = pool.conn
    conn.execute.assert_awaited_once_with(
        "SELECT pg_advisory_xact_lock($1)",
        PITWALL_BUDGET_LOCK_KEY,
    )
    conn.fetchval.assert_awaited_once_with(
        "SELECT id FROM pitwall.workloads WHERE idempotency_key = $1",
        "idem_existing",
    )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.anyio
async def test_try_launch_rejects_per_request_cap_before_pool_checkout() -> None:
    pool = _mock_pool(current_spend=Decimal("0"))
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
    )

    with pytest.raises(BudgetRejected) as exc_info:
        await gate.try_launch(
            capability_id="cap_embed",
            provider_id="prov_runpod_bge",
            estimate_usd=Decimal("2.500000"),
        )

    assert exc_info.value.reason == "per_request_cap"
    assert exc_info.value.snapshot.monthly_budget_usd == Decimal("10.000000")
    assert exc_info.value.snapshot.per_request_max_usd == Decimal("2.000000")
    assert exc_info.value.snapshot.mtd_spend_usd == Decimal("0")
    assert exc_info.value.snapshot.estimate_usd == Decimal("2.500000")
    assert exc_info.value.snapshot.budget_remaining_usd == Decimal("10.000000")
    pool.acquire.assert_not_called()


@pytest.mark.anyio
async def test_try_launch_rejects_per_token_quote_on_upper_bound_before_pool_checkout() -> None:
    pool = _mock_pool(current_spend=Decimal("0"))
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("1.000000"),
    )
    quote = quote_cost(
        capability=_capability(cost_mode="per_token"),
        provider_cost={
            "kind": "per_token",
            "per_million_input_tokens": "100.00",
            "per_million_output_tokens": "100.00",
        },
        payload={
            "input_tokens": 100,
            "output_tokens": 100,
            "max_tokens": 20_000,
        },
    )

    assert quote.estimate() == Decimal("0.020000")
    assert quote.upper_bound() == Decimal("2.010000")
    with pytest.raises(BudgetRejected) as exc_info:
        await gate.try_launch(
            capability_id="cap_token",
            provider_id="prov_token",
            estimate_usd=quote,
        )

    assert exc_info.value.reason == "per_request_cap"
    assert exc_info.value.snapshot.estimate_usd == Decimal("2.010000")
    pool.acquire.assert_not_called()


@pytest.mark.anyio
async def test_try_launch_rejects_non_positive_estimate() -> None:
    gate = BudgetGate(
        _mock_pool(current_spend=Decimal("0")),
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
    )

    with pytest.raises(ValueError, match="estimate_usd.*positive"):
        await gate.try_launch(
            capability_id="cap_embed",
            provider_id="prov_runpod_bge",
            estimate_usd=Decimal("0"),
        )


@pytest.mark.anyio
async def test_try_launch_rejects_bool_estimate_with_decimal_message() -> None:
    gate = BudgetGate(
        _mock_pool(current_spend=Decimal("0")),
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
    )

    with pytest.raises(ValueError, match="estimate_usd must be a decimal value"):
        await gate.try_launch(
            capability_id="cap_embed",
            provider_id="prov_runpod_bge",
            estimate_usd=True,
        )


@pytest.mark.anyio
async def test_try_launch_rejects_non_positive_upper_bound_estimate() -> None:
    class ZeroUpperBoundEstimate:
        def upper_bound(self) -> Decimal:
            return Decimal("0")

    gate = BudgetGate(
        _mock_pool(current_spend=Decimal("0")),
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
    )

    with pytest.raises(ValueError, match="estimate_usd must be positive"):
        await gate.try_launch(
            capability_id="cap_embed",
            provider_id="prov_runpod_bge",
            estimate_usd=ZeroUpperBoundEstimate(),
        )


@pytest.mark.anyio
async def test_current_mtd_spend_queries_workload_table() -> None:
    pool = _mock_pool(current_spend=Decimal("7.250000"))
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
    )

    spend = await gate.current_mtd_spend()

    conn = pool.conn
    assert spend == Decimal("7.250000")
    conn.fetchrow.assert_awaited_once()
    spend_sql = conn.fetchrow.await_args.args[0]
    assert "FROM pitwall.workloads" in spend_sql
    assert "SUM(COALESCE(cost_actual_usd, cost_estimate_usd))" in spend_sql
    assert "submitted_at >= date_trunc('month', now() AT TIME ZONE 'UTC')" in spend_sql
    assert "state IN ('queued','running','completed')" not in spend_sql
    conn.execute.assert_not_awaited()
    conn.fetchval.assert_not_awaited()


@pytest.mark.anyio
async def test_current_mtd_spend_returns_zero_when_query_has_no_row() -> None:
    pool = _mock_pool(current_spend=Decimal("0"))
    pool.conn.fetchrow.return_value = None
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
    )

    spend = await gate.current_mtd_spend()

    assert spend == Decimal("0")
    pool.conn.execute.assert_not_awaited()
    pool.conn.fetchval.assert_not_awaited()


@pytest.mark.anyio
async def test_current_mtd_spend_invalid_row_names_database_column() -> None:
    pool = _mock_pool(current_spend=Decimal("0"))
    pool.conn.fetchrow.return_value = {"s": "not-decimal"}
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
    )

    with pytest.raises(ValueError, match="s must be a decimal value"):
        await gate.current_mtd_spend()


@pytest.mark.anyio
async def test_current_mtd_spend_counts_all_admitted_states_preferring_actual_cost() -> None:
    pool = _mock_pool(current_spend=Decimal("7.750000"))
    gate = BudgetGate(
        pool,
        monthly_budget_usd=Decimal("10.000000"),
        per_request_max_usd=Decimal("2.000000"),
    )

    spend = await gate.current_mtd_spend()

    conn = pool.conn
    assert spend == Decimal("7.750000")
    spend_sql = conn.fetchrow.await_args.args[0]
    assert "FROM pitwall.workloads" in spend_sql
    assert "SUM(COALESCE(cost_actual_usd, cost_estimate_usd))" in spend_sql
    assert "submitted_at >= date_trunc('month', now() AT TIME ZONE 'UTC')" in spend_sql
    assert "state IN ('queued','running','completed')" not in spend_sql


@pytest.mark.anyio
async def test_concurrent_launches_cannot_race_past_monthly_cap() -> None:
    import asyncio
    from uuid import uuid4

    budget = Decimal("10.000000")
    estimate = Decimal("6.000000")

    admitted_total = [Decimal("0")]
    advisory = asyncio.Lock()
    barrier = asyncio.Barrier(2)

    class _Tx:
        def __init__(self, holds_ref: list) -> None:
            self._ref = holds_ref

        async def __aenter__(self) -> None:
            pass

        async def __aexit__(self, *exc: object) -> bool:
            if self._ref[0]:
                advisory.release()
                self._ref[0] = False
            return False

    class _Conn:
        def __init__(self) -> None:
            self._holds = [False]

        async def execute(self, sql: str, *a: object) -> str:
            if "pg_advisory_xact_lock" in sql:
                await barrier.wait()
                await advisory.acquire()
                self._holds[0] = True
            return "SELECT 1"

        async def fetchrow(self, sql: str, *a: object) -> dict[str, str]:
            return {"s": str(admitted_total[0])}

        async def fetchval(self, sql: str, *a: object) -> str:
            if "INSERT" in sql:
                admitted_total[0] += cast(Decimal, a[4])
                return str(a[0])
            return ""

        def transaction(self):
            return _Tx(self._holds)

    class _Acq:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a: object):
            return False

    class _Pool:
        def acquire(self):
            return _Acq()

    gate = BudgetGate(
        _Pool(),
        monthly_budget_usd=budget,
        per_request_max_usd=budget,
        workload_id_factory=lambda: f"wkl_{uuid4().hex[:8]}",
    )

    results: list[str] = []

    async def launch() -> None:
        try:
            await gate.try_launch(
                capability_id="cap_test",
                provider_id="prov_test",
                estimate_usd=estimate,
            )
            results.append("admitted")
        except BudgetRejected:
            results.append("rejected")

    await asyncio.gather(launch(), launch())

    assert results.count("admitted") == 1
    assert results.count("rejected") == 1
    assert admitted_total[0] <= budget


def _mock_pool_with_idempotency(
    current_spend: Decimal,
    admitted_id: str = "wkl_test",
    existing_id_for_key: str | None = None,
) -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="SELECT 1")
    conn.fetchrow = AsyncMock(return_value={"s": current_spend})
    conn.fetchval = AsyncMock(
        side_effect=lambda q, *a: existing_id_for_key if "idempotency_key" in q else admitted_id
    )
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
