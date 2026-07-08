"""Guarded workload state transitions for Pitwall async jobs.

Provides ``transition_workload`` which atomically moves a workload from one
of a set of allowed *from_states* to a target *to_state*, applying additional
column patches in the same UPDATE.  Returns ``True`` only when exactly one row
was affected (the workload was in an allowed state).
"""

from __future__ import annotations

from typing import Any

import asyncpg

_JSONB_COLUMNS = frozenset({"input", "result", "error"})


async def transition_workload(
    conn: asyncpg.Connection,
    *,
    workload_id: str,
    from_states: set[str],
    to_state: str,
    patch: dict[str, Any] | None = None,
) -> bool:
    """Atomically transition a workload between guarded states.

    Builds a single ``UPDATE … WHERE id = $N AND state IN (…)`` statement so
    the check-and-mutate is atomic inside the caller's transaction.

    Args:
        conn: An asyncpg connection that MUST be inside a transaction.
        workload_id: Primary key of the workload row.
        from_states: Set of states the workload must currently be in.
        to_state: Target state to transition to.
        patch: Optional column-value pairs to SET alongside the state change.

    Returns:
        ``True`` if exactly one row was updated (transition succeeded).
        ``False`` if no row matched (workload not in *from_states*).
    """
    set_clauses: list[str] = ["state = $1"]
    params: list[Any] = [to_state]
    idx = 2

    if patch:
        for col, val in patch.items():
            if col in _JSONB_COLUMNS:
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
        f"WHERE id = ${workload_param_idx} AND state IN ({state_placeholders})"
    )

    result = await conn.execute(query, *params)
    return bool(result == "UPDATE 1")


__all__ = [
    "transition_workload",
]
