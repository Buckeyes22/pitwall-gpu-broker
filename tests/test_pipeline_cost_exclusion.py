"""Pipeline-cost exclusion tests.

Assert legacy envelope-shaped columns never appear in Pitwall migrations.

The legacy 019_pipeline_cost.sql defined a table shaped around envelopes:
  envelope_id TEXT PK, pipeline TEXT, status IN ('success','failed','partial'),
  cost_usd, wall_clock_ms, step_breakdown JSONB, token_breakdown JSONB,
  started_at, finished_at, git_sha, model_mix TEXT[]

Pitwall's billing grain is (request_id, customer_id, started_at) — not envelopes.
The entire table, its column vocabulary, and its index patterns must be absent.

Additionally, the legacy system had per-feature cost ledgers (triage_cost, vlm_cost,
training_cost) that predate the unified cloud_cost design and are irrelevant
to a broker. Their column patterns are also excluded.

Spec references (v0.3):
  - 1.5 migration table: "Do not port. Shaped around legacy envelopes."
  - 12 migration table: "(DO NOT port) Legacy envelope-shaped; redesign or skip."
  - Inventory 3.4: pipeline_cost does NOT map to Pitwall.
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


def _migration_filenames() -> list[str]:
    return [p.name for p in sorted(_MIGRATION_DIR.glob("*.sql"))]


class TestNoPipelineCostTable:
    """The legacy pipeline_cost table must not exist in Pitwall."""

    def test_pipeline_cost_table_absent(self) -> None:
        combined = _all_migration_sql().lower()
        assert "pipeline_cost" not in combined, (
            "Legacy pipeline_cost table (envelope-shaped billing) must not be ported"
        )

    def test_no_pipeline_cost_migration_file(self) -> None:
        filenames = _migration_filenames()
        for fn in filenames:
            assert "pipeline_cost" not in fn.lower(), (
                f"Migration file '{fn}' references pipeline_cost"
            )

    def test_no_pipeline_cost_index_pattern(self) -> None:
        combined = _all_migration_sql().lower()
        assert "idx_pipeline_cost" not in combined, (
            "Legacy pipeline_cost index pattern must not appear in Pitwall migrations"
        )


class TestNoEnvelopeShapedColumns:
    """Columns specific to the legacy envelope-shaped pipeline_cost must not leak
    into any Pitwall migration. These columns are structurally coupled to
    the envelope_id PK and step/token breakdown model."""

    ENVELOPE_COLUMNS = (
        "envelope_id",
        "step_breakdown",
        "token_breakdown",
    )

    PIPELINE_COST_ONLY_COLUMNS = (
        "model_mix",
        "wall_clock_ms",
        "git_sha",
    )

    @pytest.mark.parametrize("column", ENVELOPE_COLUMNS)
    def test_envelope_column_absent(self, column: str) -> None:
        combined = _all_migration_sql()
        assert column not in combined, (
            f"Envelope-shaped cost column '{column}' must not appear in Pitwall migrations"
        )

    @pytest.mark.parametrize("column", PIPELINE_COST_ONLY_COLUMNS)
    def test_pipeline_cost_column_absent(self, column: str) -> None:
        combined = _stripped_migration_sql()
        assert column not in combined, (
            f"Legacy pipeline_cost column '{column}' must not appear in Pitwall migration code"
        )

    def test_no_pipeline_cost_status_check(self) -> None:
        code = _stripped_migration_sql().lower()
        assert "'success', 'failed', 'partial'" not in code, (
            "Legacy pipeline_cost status CHECK (success/failed/partial) must not appear"
        )
        assert "'success','failed','partial'" not in code, (
            "Legacy pipeline_cost status CHECK (success/failed/partial) must not appear"
        )


class TestNoPerFeatureCostLedgers:
    """The legacy system had per-feature cost ledgers (triage_cost, vlm_cost, training_cost)
    that predate the unified cloud_cost design. They are irrelevant to a broker."""

    FORBIDDEN_TABLES = ("triage_cost", "vlm_cost", "training_cost")

    @pytest.mark.parametrize("table", FORBIDDEN_TABLES)
    def test_per_feature_cost_table_absent(self, table: str) -> None:
        combined = _all_migration_sql().lower()
        assert table not in combined, (
            f"Legacy per-feature ledger table '{table}' must not be ported"
        )

    def test_no_intake_envelopes_reference(self) -> None:
        combined = _all_migration_sql().lower()
        assert "intake_envelopes" not in combined, (
            "Legacy intake_envelopes FK reference must not appear in Pitwall migrations"
        )

    def test_no_training_cost_check_patterns(self) -> None:
        code = _stripped_migration_sql().lower()
        for pattern in (
            "persona in (",
            "stage in (",
            "'crew_chief'",
            "'race_engineer'",
            "'driving_coach'",
        ):
            assert pattern not in code, (
                f"Legacy training_cost CHECK pattern '{pattern}' must not appear"
            )


class TestNoPipelineCostInPythonSource:
    """Python source under src/pitwall must not reference pipeline_cost
    or envelope-shaped billing concepts."""

    FORBIDDEN_TERMS = (
        "pipeline_cost",
        "PipelineCost",
        "step_breakdown",
        "token_breakdown",
        "envelope_id",
        "model_mix",
    )

    @pytest.mark.parametrize("term", FORBIDDEN_TERMS)
    def test_pipeline_cost_term_absent_from_source(self, term: str) -> None:
        src_dir = _REPO_ROOT / "src"
        if not src_dir.exists():
            pytest.skip("src directory not found")
        for py_file in src_dir.rglob("*.py"):
            content = py_file.read_text()
            assert term not in content, (
                f"Legacy pipeline_cost term '{term}' found in {py_file.relative_to(_REPO_ROOT)}"
            )


class TestCostDailyUsesPitwallGrain:
    """Pitwall's cost_daily must use the Pitwall billing grain
    (day, capability_class, provider_type), not the legacy envelope grain."""

    def test_cost_daily_has_no_envelope_columns(self) -> None:
        cost_daily_path = _MIGRATION_DIR / "0009_cost_daily.sql"
        if not cost_daily_path.exists():
            pytest.skip("0009_cost_daily.sql not yet created")
        content = cost_daily_path.read_text().lower()
        for forbidden in ("envelope_id", "pipeline", "step_breakdown", "token_breakdown"):
            assert forbidden not in content, (
                f"cost_daily must not contain legacy envelope column '{forbidden}'"
            )

    def test_cost_daily_uses_daily_grain(self) -> None:
        cost_daily_path = _MIGRATION_DIR / "0009_cost_daily.sql"
        if not cost_daily_path.exists():
            pytest.skip("0009_cost_daily.sql not yet created")
        content = cost_daily_path.read_text().lower()
        assert "day" in content, "cost_daily must have a 'day' column"
        assert "cost_usd" in content, "cost_daily must track cost_usd"
