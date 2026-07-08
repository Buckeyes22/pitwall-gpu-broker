"""Tests for migration discovery, checksums, and drift detection."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pitwall.migrations import (
    MigrationRecord,
    detect_drift,
    discover_migrations,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_DIR = _REPO_ROOT / "db" / "migrations"


class TestDiscoverMigrations:
    def test_default_discovery_reads_packaged_or_checkout_resources(self) -> None:
        records = discover_migrations()
        assert records
        assert all(record.sql.strip() for record in records)

    def test_discovers_all_sql_files(self) -> None:
        records = discover_migrations(_MIGRATION_DIR)
        filenames = [r.filename for r in records]
        assert filenames == sorted(filenames), "migrations must be sorted lexically"

    def test_returns_migration_records(self) -> None:
        records = discover_migrations(_MIGRATION_DIR)
        assert len(records) >= 10
        for rec in records:
            assert isinstance(rec, MigrationRecord)
            assert rec.filename.endswith(".sql")
            assert rec.version == Path(rec.filename).stem
            assert len(rec.checksum) == 64

    def test_checksum_is_sha256_of_file_contents(self, tmp_path: Path) -> None:
        sql_file = tmp_path / "0001_test.sql"
        sql_file.write_text("CREATE TABLE foo (id int);")
        records = discover_migrations(tmp_path)
        expected = hashlib.sha256(sql_file.read_bytes()).hexdigest()
        assert records[0].checksum == expected

    def test_sorts_lexically_by_filename(self, tmp_path: Path) -> None:
        (tmp_path / "0003_charlie.sql").write_text("C;")
        (tmp_path / "0001_alpha.sql").write_text("A;")
        (tmp_path / "0002_bravo.sql").write_text("B;")
        records = discover_migrations(tmp_path)
        versions = [r.version for r in records]
        assert versions == ["0001_alpha", "0002_bravo", "0003_charlie"]

    def test_ignores_non_sql_files(self, tmp_path: Path) -> None:
        (tmp_path / "0001_valid.sql").write_text("SELECT 1;")
        (tmp_path / "notes.txt").write_text("not a migration")
        records = discover_migrations(tmp_path)
        assert len(records) == 1
        assert records[0].filename == "0001_valid.sql"

    def test_raises_on_missing_directory(self) -> None:
        with pytest.raises(FileNotFoundError):
            discover_migrations("/nonexistent/path/migrations")

    def test_empty_directory_returns_empty_list(self, tmp_path: Path) -> None:
        assert discover_migrations(tmp_path) == []


class TestDetectDrift:
    def test_no_drift_when_checksums_match(self) -> None:
        records = [
            MigrationRecord("0001", "0001.sql", "abc123"),
            MigrationRecord("0002", "0002.sql", "def456"),
        ]
        applied = {"0001": "abc123", "0002": "def456"}
        assert detect_drift(records, applied) == []

    def test_detects_changed_checksum(self) -> None:
        records = [
            MigrationRecord("0001", "0001.sql", "newhash"),
        ]
        applied = {"0001": "oldhash"}
        drifts = detect_drift(records, applied)
        assert len(drifts) == 1
        assert drifts[0].version == "0001"
        assert drifts[0].recorded_checksum == "oldhash"
        assert drifts[0].current_checksum == "newhash"

    def test_new_migration_is_not_drift(self) -> None:
        records = [
            MigrationRecord("0001", "0001.sql", "abc"),
            MigrationRecord("0002", "0002.sql", "def"),
        ]
        applied = {"0001": "abc"}
        drifts = detect_drift(records, applied)
        assert drifts == []

    def test_multiple_drifts(self) -> None:
        records = [
            MigrationRecord("0001", "0001.sql", "a1"),
            MigrationRecord("0002", "0002.sql", "b2"),
            MigrationRecord("0003", "0003.sql", "c3"),
        ]
        applied = {"0001": "a1", "0002": "changed", "0003": "also_changed"}
        drifts = detect_drift(records, applied)
        assert len(drifts) == 2
        drifted_versions = {d.version for d in drifts}
        assert drifted_versions == {"0002", "0003"}


class TestMigrationRecordAgainstRealFiles:
    def test_all_migrations_have_stable_checksums(self) -> None:
        records = discover_migrations(_MIGRATION_DIR)
        for rec in records:
            path = _MIGRATION_DIR / rec.filename
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            assert rec.checksum == expected, f"checksum mismatch for {rec.filename}"

    def test_version_stem_matches_filename(self) -> None:
        records = discover_migrations(_MIGRATION_DIR)
        for rec in records:
            assert rec.version == Path(rec.filename).stem
