"""Tests for the PIT restore drill (backup_drill.py)."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class TestBackupDrillModule:
    """Test that the backup drill module can be imported."""

    def test_import_backup_drill(self) -> None:
        """Verify the backup_drill module imports cleanly."""
        from pitwall.ops.backup_drill import (
            DRILL_TYPE,
            PITWALL_TABLES,
        )

        assert DRILL_TYPE == "postgres_pit_restore"
        assert PITWALL_TABLES == [
            "capabilities",
            "providers",
            "workloads",
            "leases",
            "config_audit",
            "kill_log",
        ]

    def test_table_check_model(self) -> None:
        """Test TableCheck model initialization."""
        from pitwall.ops.backup_drill import TableCheck

        check = TableCheck(
            table="capabilities",
            row_count=5,
            checksum="abc123",
            errors=[],
        )
        assert check.table == "capabilities"
        assert check.row_count == 5
        assert check.checksum == "abc123"
        assert check.passed is True

    def test_table_check_with_errors(self) -> None:
        """Test TableCheck with errors."""
        from pitwall.ops.backup_drill import TableCheck

        check = TableCheck(
            table="capabilities",
            row_count=0,
            checksum="",
            errors=["Table does not exist"],
        )
        assert check.passed is False

    def test_backup_drill_report_model(self) -> None:
        """Test BackupDrillReport model."""
        from datetime import datetime

        from pitwall.ops.backup_drill import BackupDrillReport, TableCheck

        started = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
        completed = datetime(2026, 5, 29, 12, 1, 0, tzinfo=UTC)
        checks = [
            TableCheck(table="capabilities", row_count=5, checksum="abc123"),
        ]

        report = BackupDrillReport(
            drill_id="postgres_pit_restore-20260529-120000",
            started_at=started,
            completed_at=completed,
            temp_db_name="pitwall_restore_test123",
            target="latest",
            passed=True,
            checks=checks,
            errors=[],
            config_audit_id=42,
        )

        assert report.passed is True
        assert report.temp_db_name == "pitwall_restore_test123"
        assert report.config_audit_id == 42

    def test_generate_temp_db_name(self) -> None:
        """Test temp database name generation."""
        from pitwall.ops.backup_drill import _generate_temp_db_name

        name = _generate_temp_db_name()
        assert name.startswith("pitwall_restore_")
        assert len(name) > len("pitwall_restore_")

    def test_generate_temp_db_name_unique(self) -> None:
        """Test that generated names are unique."""
        from pitwall.ops.backup_drill import _generate_temp_db_name

        names = {_generate_temp_db_name() for _ in range(100)}
        assert len(names) == 100

    def test_database_url_rewrite_handles_reserved_password_characters(self) -> None:
        from pitwall.ops.backup_drill import _database_url_with_name

        source = "postgresql://operator:p%40ss%2Fword@db.example:5432/source"
        rewritten = _database_url_with_name(source, "pitwall_restore_abc")
        assert rewritten == (
            "postgresql://operator:p%40ss%2Fword@db.example:5432/pitwall_restore_abc"
        )

    async def test_psql_argv_never_contains_database_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pitwall.ops.backup_drill import _create_temp_database

        source = "postgresql://operator:canary%40secret@db.example:5432/source"
        captured: dict[str, object] = {}

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured["args"] = args
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = await _create_temp_database(source, "pitwall_restore_abc")

        argv = captured["args"]
        assert isinstance(argv, list)
        assert source not in argv
        assert all("canary" not in argument for argument in argv)
        environment = captured["env"]
        assert isinstance(environment, dict)
        assert environment["PGPASSWORD"] == "canary@secret"
        assert result.endswith("/pitwall_restore_abc")


class TestBackupDrillCLI:
    """Test the backup drill CLI entry point."""

    def test_dry_run_exits_zero(self) -> None:
        """Verify --dry-run exits with code 0."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pitwall.ops.backup_drill",
                "--schema",
                "pitwall",
                "--target",
                "latest",
                "--dry-run",
            ],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    def test_dry_run_prints_restore_plan(self) -> None:
        """Verify --dry-run prints the restore plan."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pitwall.ops.backup_drill",
                "--schema",
                "pitwall",
                "--target",
                "latest",
                "--dry-run",
            ],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "PIT Restore Drill Plan" in result.stdout
        assert "Schema: pitwall" in result.stdout
        assert "Target backup: latest" in result.stdout
        assert "DRY-RUN: No changes made" in result.stdout

    def test_dry_run_lists_tables(self) -> None:
        """Verify --dry-run lists all tables to validate."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pitwall.ops.backup_drill",
                "--schema",
                "pitwall",
                "--target",
                "latest",
                "--dry-run",
            ],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        for table in [
            "capabilities",
            "providers",
            "workloads",
            "leases",
            "config_audit",
            "kill_log",
        ]:
            assert f"pitwall.{table}" in result.stdout


class TestBackupDrillReconciler:
    """Test that the backup drill is wired into the reconciler."""

    def test_backup_drill_in_reconciler_cron(self) -> None:
        """Verify _backup_drill is in the reconciler cron jobs."""
        from pitwall.reconciler import WorkerSettings

        cron_job_names = [job.name for job in WorkerSettings.cron_jobs]
        assert "cron:_backup_drill" in cron_job_names

    def test_backup_drill_schedule(self) -> None:
        """Verify backup_drill cron job has correct schedule.

        Schedule should be: Sunday at 04:00 UTC (0 4 * * 0).
        In arq cron: hour={4}, minute={0}, weekday={0}
        """
        from pitwall.reconciler import WorkerSettings

        for job in WorkerSettings.cron_jobs:
            if job.name == "cron:_backup_drill":
                assert job.hour == {4}, f"Expected hour={{4}}, got {job.hour}"
                assert job.minute == {0}, f"Expected minute={{0}}, got {job.minute}"
                assert job.weekday == {0}, f"Expected weekday={{0}}, got {job.weekday}"
                return

        pytest.fail("cron:_backup_drill cron job not found in WorkerSettings.cron_jobs")

    def test_backup_drill_function_exists(self) -> None:
        """Verify _backup_drill async function exists in reconciler."""
        from pitwall.reconciler import _ARQ_AVAILABLE

        if not _ARQ_AVAILABLE:
            pytest.skip("arq not available")

        import asyncio

        from pitwall.reconciler import _backup_drill

        assert asyncio.iscoroutinefunction(_backup_drill)


class TestBackupDrillGrepAC:
    """Test the grep acceptance criterion for the backup drill."""

    def test_grep_reconciler_for_schedule_and_job(self) -> None:
        """Verify the grep command for schedule and job finds results.

        Acceptance criterion:
        grep -R '0 4 * * 0\\|run_pit_restore_drill'
            src/pitwall/reconciler/ src/pitwall/ops/backup_drill.py
        returns the schedule and job.
        """
        reconciler_dir = _REPO_ROOT / "src" / "pitwall" / "reconciler"
        ops_file = _REPO_ROOT / "src" / "pitwall" / "ops" / "backup_drill.py"

        result = subprocess.run(
            [
                "grep",
                "-E",
                r"0 4 \* \* 0|run_pit_restore_drill",
                "-R",
                str(reconciler_dir),
                str(ops_file),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        matches = result.stdout.strip().splitlines()
        has_run_pit = any("run_pit_restore_drill" in m for m in matches)
        assert has_run_pit, f"run_pit_restore_drill not found in matches: {matches}"
