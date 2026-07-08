"""Normalized workload output for MCP tool responses.

All MCP inference and job tools return a consistent JSON shape containing
cost, provider, state, result, and trace fields via ``normalize_workload_output``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pitwall.core.models import Workload


def _decimal_to_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)


def normalize_workload_output(workload: Workload) -> dict[str, Any]:
    """Return a structured workload dict with cost, provider, state, result, and trace.

    Every MCP tool that operates on a Workload record returns this shape so
    callers get a uniform contract regardless of which tool was invoked.
    """
    return {
        "workload_id": workload.id,
        "cost": {
            "estimate_usd": _decimal_to_str(workload.cost_estimate_usd),
            "actual_usd": _decimal_to_str(workload.cost_actual_usd),
        },
        "provider_id": workload.provider_id,
        "state": workload.state.value if hasattr(workload.state, "value") else workload.state,
        "result": workload.result,
        "trace_id": workload.langfuse_trace_id,
    }


__all__ = ["normalize_workload_output"]
