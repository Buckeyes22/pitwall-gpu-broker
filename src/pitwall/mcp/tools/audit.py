"""Audit log tool for the MCP surface.

This tool exposes filtered reads of the config mutation audit trail,
mirroring the GET /v1/admin/audit-log REST endpoint.

Audit entries record who (actor) did what (action) to which entity
(entity_type + entity_id), with before/after snapshots (old_value, new_value).
"""

from __future__ import annotations

from typing import Any

from pitwall.db import get_pool
from pitwall.db.repository import list_audit as _list_audit


async def pitwall_audit_log(
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return config mutation audit entries, optionally filtered.

    Mirrors GET /v1/admin/audit-log.

    Args:
        entity_type: Optional filter for entity type (e.g., 'capability', 'provider').
        entity_id: Optional filter for specific entity ID.
        action: Optional filter for action (e.g., 'create', 'update', 'disable').
        limit: Maximum number of entries to return (default 50).

    Returns:
        A dict with ``entries`` (list of audit entry dicts), each containing:
        id, actor, action, entity_type, entity_id, old_value, new_value,
        change_reason, and created_at.
    """
    pool = await get_pool()
    entries = await _list_audit(
        pool,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        limit=limit,
    )
    return {
        "entries": [
            {
                "id": e.id,
                "actor": e.actor,
                "action": e.action,
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "old_value": e.old_value,
                "new_value": e.new_value,
                "change_reason": e.change_reason,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ]
    }


__all__ = ["pitwall_audit_log"]
