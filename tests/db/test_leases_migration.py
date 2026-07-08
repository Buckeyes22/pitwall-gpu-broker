"""static contract for the pod lease migration."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION = _REPO_ROOT / "db" / "migrations" / "0004_leases.sql"
_SQL = _MIGRATION.read_text()


def test_leases_migration_creates_spec_table() -> None:
    assert "CREATE TABLE pitwall.leases (" in _SQL

    expected_columns = {
        "id": "TEXT PRIMARY KEY",
        "provider_id": "TEXT NOT NULL",
        "runpod_pod_id": "TEXT NOT NULL",
        "state": "TEXT NOT NULL CHECK",
        "created_at": "TIMESTAMPTZ NOT NULL",
        "expires_at": "TIMESTAMPTZ NOT NULL",
        "renewal_policy": "TEXT NOT NULL",
        "endpoints": "JSONB",
        "readiness": "JSONB",
        "cost_accrued_usd": "NUMERIC(12,6)",
        "last_health_at": "TIMESTAMPTZ",
        "terminated_at": "TIMESTAMPTZ",
        "terminated_reason": "TEXT",
    }
    for column, definition in expected_columns.items():
        assert re.search(
            rf"^\s*{column}\s+{re.escape(definition)}(?=\s|,|$)",
            _SQL,
            flags=re.MULTILINE,
        ), f"missing lease column {column} {definition}"


def test_leases_migration_state_machine_matches_spec() -> None:
    states = re.findall(r"'([^']+)'", _state_check_sql())

    assert states == [
        "creating",
        "waiting_runtime",
        "waiting_probe",
        "active",
        "stopping",
        "stopped",
        "failed",
        "expired",
    ]


def test_leases_migration_has_active_expiry_index() -> None:
    assert (
        "CREATE INDEX idx_leases_expires ON pitwall.leases(state, expires_at)\n"
        "  WHERE state = 'active';"
    ) in _SQL


def test_leases_migration_enforces_active_readiness_signals() -> None:
    assert "CREATE FUNCTION pitwall.lease_active_has_readiness_signals" in _SQL
    assert "CONSTRAINT leases_active_readiness_signals CHECK" in _SQL

    for readiness_key in (
        "runtime_seen_at",
        "port_mappings_seen_at",
        "probe_passed_at",
    ):
        assert f"lease_readiness ->> '{readiness_key}'" in _SQL


def _state_check_sql() -> str:
    match = re.search(
        r"state\s+TEXT\s+NOT\s+NULL\s+CHECK\s+\(state\s+IN\s*(.*?)\),",
        _SQL,
        flags=re.DOTALL,
    )
    assert match is not None, "missing leases.state CHECK"
    return match.group(1)
