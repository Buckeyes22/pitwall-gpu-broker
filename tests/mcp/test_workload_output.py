"""Tests for normalized workload output.

Verify that normalize_workload_output returns structured JSON with cost,
provider, state, result, and trace fields for every workload lifecycle state.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from pitwall.core.enums import WorkloadState
from pitwall.core.models import Workload
from pitwall.mcp.tools.output import normalize_workload_output

_TEST_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)

_REQUIRED_KEYS = {"workload_id", "cost", "provider_id", "state", "result", "trace_id"}
_COST_KEYS = {"estimate_usd", "actual_usd"}


def _make_workload(
    *,
    state: WorkloadState = WorkloadState.QUEUED,
    cost_estimate_usd: Decimal | None = None,
    cost_actual_usd: Decimal | None = None,
    result: dict[str, Any] | None = None,
    langfuse_trace_id: str | None = None,
    **overrides: Any,
) -> Workload:
    defaults: dict[str, Any] = {
        "id": "wkl_test01",
        "capability_id": "cap_llm_qwen3_32b",
        "provider_id": "prov_qwen3_32b",
        "type": "openai_passthrough",
        "state": state,
        "submitted_at": _TEST_NOW,
        "cost_estimate_usd": cost_estimate_usd,
        "cost_actual_usd": cost_actual_usd,
        "result": result,
        "langfuse_trace_id": langfuse_trace_id,
    }
    defaults.update(overrides)
    return Workload(**defaults)


class TestNormalizeWorkloadOutput:
    def test_returns_all_required_top_level_keys(self) -> None:
        wl = _make_workload()
        out = normalize_workload_output(wl)
        assert set(out.keys()) >= _REQUIRED_KEYS

    def test_cost_contains_estimate_and_actual(self) -> None:
        wl = _make_workload()
        out = normalize_workload_output(wl)
        assert set(out["cost"].keys()) >= _COST_KEYS

    def test_cost_estimate_usd_as_string(self) -> None:
        wl = _make_workload(cost_estimate_usd=Decimal("0.001234"))
        out = normalize_workload_output(wl)
        assert out["cost"]["estimate_usd"] == "0.001234"

    def test_cost_actual_usd_as_string(self) -> None:
        wl = _make_workload(cost_actual_usd=Decimal("0.005000"))
        out = normalize_workload_output(wl)
        assert out["cost"]["actual_usd"] == "0.005000"

    def test_cost_none_when_not_set(self) -> None:
        wl = _make_workload()
        out = normalize_workload_output(wl)
        assert out["cost"]["estimate_usd"] is None
        assert out["cost"]["actual_usd"] is None

    def test_provider_id_from_workload(self) -> None:
        wl = _make_workload()
        out = normalize_workload_output(wl)
        assert out["provider_id"] == "prov_qwen3_32b"

    def test_state_is_string_value(self) -> None:
        wl = _make_workload(state=WorkloadState.RUNNING)
        out = normalize_workload_output(wl)
        assert out["state"] == "running"
        assert isinstance(out["state"], str)

    def test_state_for_each_workload_state(self) -> None:
        for ws in WorkloadState:
            wl = _make_workload(state=ws)
            out = normalize_workload_output(wl)
            assert out["state"] == ws.value

    def test_result_dict_passthrough(self) -> None:
        result = {"dense": [[0.1, 0.2]], "sparse": {"token": 1}}
        wl = _make_workload(result=result)
        out = normalize_workload_output(wl)
        assert out["result"] == result

    def test_result_none_when_not_set(self) -> None:
        wl = _make_workload()
        out = normalize_workload_output(wl)
        assert out["result"] is None

    def test_trace_id_from_workload(self) -> None:
        wl = _make_workload(langfuse_trace_id="trace-abc-123")
        out = normalize_workload_output(wl)
        assert out["trace_id"] == "trace-abc-123"

    def test_trace_id_none_when_not_set(self) -> None:
        wl = _make_workload()
        out = normalize_workload_output(wl)
        assert out["trace_id"] is None

    def test_workload_id_from_workload(self) -> None:
        wl = _make_workload()
        out = normalize_workload_output(wl)
        assert out["workload_id"] == "wkl_test01"

    def test_completed_workload_with_full_data(self) -> None:
        wl = _make_workload(
            state=WorkloadState.COMPLETED,
            cost_estimate_usd=Decimal("0.002000"),
            cost_actual_usd=Decimal("0.001850"),
            result={"status": "ok", "output": [1, 2, 3]},
            langfuse_trace_id="trace-xyz-789",
            execution_ms=150,
        )
        out = normalize_workload_output(wl)
        assert out["workload_id"] == "wkl_test01"
        assert out["cost"]["estimate_usd"] == "0.002000"
        assert out["cost"]["actual_usd"] == "0.001850"
        assert out["provider_id"] == "prov_qwen3_32b"
        assert out["state"] == "completed"
        assert out["result"] == {"status": "ok", "output": [1, 2, 3]}
        assert out["trace_id"] == "trace-xyz-789"

    def test_failed_workload(self) -> None:
        wl = _make_workload(
            state=WorkloadState.FAILED,
            error={"type": "timeout", "message": "upstream timed out"},
        )
        out = normalize_workload_output(wl)
        assert out["state"] == "failed"
        assert out["result"] is None
