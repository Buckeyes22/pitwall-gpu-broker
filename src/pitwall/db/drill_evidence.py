"""Repository for drill evidence persistence.

Provides two persistence paths for every completed drill:
  1. A row in ``pitwall.config_audit`` with entity_type='drill'
  2. A JSON report written to disk at a configurable path

Both are written on every drill completion so evidence is never lost
even if the disk write fails (the DB row is the authoritative record).

Typical usage from an Arq scheduled job::

    from pitwall.db.drill_evidence import persist_drill_evidence, write_drill_json_report

    async def run_pg_restore_drill(ctx: dict) -> None:
        pool = ctx.get("db_pool")
        drill_type = "postgres_pit_restore"
        drill_id = f"{drill_type}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"

        # ... perform drill checks ...

        evidence = {
            "drill_id": drill_id,
            "drill_type": drill_type,
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "passed": True,
            "checks": [...],
            "errors": [],
        }

        # Persist to DB (authoritative)
        await persist_drill_evidence(
            pool,
            drill_id=drill_id,
            drill_type=drill_type,
            evidence=evidence,
        )

        # Write JSON report to disk (human-readable artifact)
        write_drill_json_report(evidence, drill_type=drill_type)
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg


async def persist_drill_evidence(
    pool: asyncpg.Pool,
    drill_id: str,
    drill_type: str,
    evidence: dict[str, Any],
    *,
    actor: str = "system",
    change_reason: str | None = None,
) -> int:
    """Insert a drill evidence row into ``pitwall.config_audit``.

    Uses entity_type='drill' so drill evidence is queryable alongside
    other config mutations via the existing audit trail.

    Args:
        pool: asyncpg connection pool.
        drill_id: Unique identifier for this drill run
                  (e.g. ``postgres_pit_restore-20260528-120000``).
        drill_type: Short type name for the drill
                    (e.g. ``postgres_pit_restore``, ``kill_switch``).
        evidence: Full drill result dict — will be stored as JSONB in
                  ``new_value``.
        actor: Who/what initiated the drill. Defaults to ``system``
               for automated Arq scheduled jobs.
        change_reason: Optional human-readable reason for the drill
                      (e.g. "weekly scheduled PIT restore validation").

    Returns:
        The inserted row ``id`` from ``pitwall.config_audit``.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO pitwall.config_audit
                (actor, action, entity_type, entity_id, new_value, change_reason)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
            RETURNING id
            """,
            actor,
            "create",
            "drill",
            drill_id,
            json.dumps(evidence),
            change_reason,
        )
        assert row is not None
        return int(row["id"])


def write_drill_json_report(
    evidence: dict[str, Any],
    *,
    drill_type: str | None = None,
    output_dir: str | Path | None = None,
) -> Path:
    """Write a drill evidence dict to a timestamped JSON file.

    The file is written to ``{output_dir}/{drill_type}-{timestamp}.json``.
    If *output_dir* is not provided, defaults to the ``PITWALL_DRILL_ARTIFACTS_DIR``
    environment variable, or ``artifacts/drils`` relative to the repo root.

    Args:
        evidence: Drill result dict to serialize as JSON.
        drill_type: Optional drill type name used in the filename.
                    If not provided, the file is named with the ``drill_type``
                    from *evidence* or ``unknown``.
        output_dir: Optional output directory. Defaults to env var or repo-path.

    Returns:
        Path to the written file.

    Raises:
        OSError: If the output directory cannot be created or the file
                 cannot be written.
    """
    if output_dir is None:
        output_dir = os.environ.get(
            "PITWALL_DRILL_ARTIFACTS_DIR",
            str(Path(__file__).resolve().parents[4] / "artifacts" / "drills"),
        )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    type_slug = drill_type or evidence.get("drill_type", "unknown")
    filename = f"{type_slug}-{ts}.json"
    file_path = output_path / filename

    file_path.write_text(
        json.dumps(evidence, indent=2, default=str),
        encoding="utf-8",
    )
    return file_path


async def get_drill_evidence(
    pool: asyncpg.Pool,
    *,
    drill_id: str | None = None,
    drill_type: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch drill evidence rows from ``pitwall.config_audit``.

    Args:
        pool: asyncpg connection pool.
        drill_id: If provided, return only the row with this entity_id.
        drill_type: If provided, filter rows whose new_value contains a
                    matching ``drill_type`` field.
        since: If provided, return only rows created at or after this time.
        limit: Maximum number of rows to return (default 100).

    Returns:
        List of dicts representing config_audit rows with entity_type='drill'.
    """
    conditions: list[str] = ["entity_type = $1"]
    params: list[Any] = ["drill"]
    param_idx = 2

    if drill_id is not None:
        conditions.append(f"entity_id = ${param_idx}")
        params.append(drill_id)
        param_idx += 1

    if since is not None:
        conditions.append(f"created_at >= ${param_idx}")
        params.append(since)
        param_idx += 1

    where_clause = " AND ".join(conditions)
    query = f"""
        SELECT id, actor, entity_type, entity_id, new_value,
               change_reason, created_at
        FROM pitwall.config_audit
        WHERE {where_clause}
        ORDER BY created_at DESC
        LIMIT ${param_idx}
    """
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    results: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        new_value = record.pop("new_value")
        if isinstance(new_value, str):
            new_value = json.loads(new_value)
        record["new_value"] = new_value

        if drill_type is not None and new_value.get("drill_type") != drill_type:
            continue

        results.append(record)

    return results
