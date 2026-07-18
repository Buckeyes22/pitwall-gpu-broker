"""Do-not-port guard tests.

Assert that the Pitwall migration suite does not carry the narrow
workload/provider CHECK enums or envelope-shaped cost columns from the
legacy schema.

Spec references (v0.3):
  - 1.5: 015_cloud_cost.sql  — widen workload/provider CHECKs
  - 1.5: 019_pipeline_cost.sql — "Do not port. Shaped around legacy envelopes."
  - 12:   migration table — Drop workload IN ('re-embedding',...),
          drop provider IN ('runpod','vast','crusoe').
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"


def _all_migration_sql() -> str:
    parts: list[str] = []
    for p in sorted(_MIGRATION_DIR.glob("*.sql")):
        parts.append(p.read_text())
    return "\n".join(parts)


def _stripped_migration_sql() -> str:
    lines: list[str] = []
    for raw_line in _all_migration_sql().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines)


class TestNoLegacyWorkloadEnum:
    """The legacy cloud_cost table constrained workload to four names.
    Pitwall widened this to a free-form TEXT column."""

    FORBIDDEN_WORKLOAD_VALUES = ("re-embedding", "ocr-burst", "sft-generation", "misc")

    @pytest.mark.parametrize("value", FORBIDDEN_WORKLOAD_VALUES)
    def test_legacy_workload_value_absent_from_checks(self, value: str) -> None:
        combined = _all_migration_sql()
        assert value not in combined, (
            f"Legacy workload enum value '{value}' must not appear in Pitwall migrations"
        )

    def test_no_workload_check_constraint(self) -> None:
        combined = _all_migration_sql().lower()
        assert "workload in (" not in combined, (
            "Pitwall must not carry a narrow workload IN (...) CHECK constraint"
        )


class TestNoLegacyProviderEnum:
    """The legacy cloud_cost table constrained provider to three names.
    Pitwall's providers table uses provider_type with its own wider enum.
    'runpod' legitimately appears in column names (runpod_job_id etc.),
    so we check for it only inside CHECK-style patterns."""

    FORBIDDEN_PROVIDER_VALUES = ("vast", "crusoe")

    @pytest.mark.parametrize("value", FORBIDDEN_PROVIDER_VALUES)
    def test_legacy_provider_value_absent_from_migrations(self, value: str) -> None:
        combined = _all_migration_sql()
        assert value not in combined, (
            f"Legacy provider enum value '{value}' must not appear in Pitwall migrations"
        )

    def test_runpod_not_in_provider_check(self) -> None:
        code = _stripped_migration_sql().lower()
        assert "'runpod'" not in code and '"runpod"' not in code, (
            "The string literal 'runpod' must not appear in CHECK constraints"
        )

    def test_no_provider_check_with_legacy_values(self) -> None:
        code = _stripped_migration_sql().lower()
        assert "provider in (" not in code, (
            "Pitwall must not carry a narrow provider IN (...) CHECK constraint"
        )


class TestNoEnvelopeShapedCostColumns:
    """The legacy 019_pipeline_cost.sql is envelope-shaped (envelope_id PK,
    step_breakdown, token_breakdown).  Pitwall must not port this table
    or its column pattern."""

    FORBIDDEN_COLUMNS = ("envelope_id", "step_breakdown", "token_breakdown")

    @pytest.mark.parametrize("column", FORBIDDEN_COLUMNS)
    def test_envelope_column_absent(self, column: str) -> None:
        combined = _all_migration_sql()
        assert column not in combined, (
            f"Envelope-shaped cost column '{column}' must not appear in Pitwall migrations"
        )

    def test_no_pipeline_cost_table(self) -> None:
        combined = _all_migration_sql().lower()
        assert "pipeline_cost" not in combined, (
            "Legacy pipeline_cost table (envelope-shaped billing) must not be ported"
        )

    def test_no_triage_cost_or_vlm_cost_or_training_cost_tables(self) -> None:
        combined = _all_migration_sql().lower()
        for table in ("triage_cost", "vlm_cost", "training_cost"):
            assert table not in combined, (
                f"Legacy per-feature ledger table '{table}' must not be ported"
            )


class TestNoLegacyStateNames:
    """The legacy schema used 'launching' and 'killed' as workload states.
    Pitwall uses a different state machine: queued/running/completed/
    failed/cancelled/timed_out.

    'launching' and 'killed' may appear in SQL comments; only check
    the code (non-comment) lines."""

    FORBIDDEN_STATE_VALUES = ("launching", "killed")

    @pytest.mark.parametrize("state", FORBIDDEN_STATE_VALUES)
    def test_legacy_state_absent_from_code(self, state: str) -> None:
        code = _stripped_migration_sql()
        assert state not in code, (
            f"Legacy state '{state}' must not appear in Pitwall state CHECK constraints"
        )
