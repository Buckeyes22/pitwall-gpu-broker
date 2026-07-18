"""Migration discovery, checksums, and drift detection for Pitwall.

Reads ``db/migrations/*.sql``, sorts lexically, computes SHA-256 checksums,
and rejects drift when a previously-applied migration's checksum has changed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class MigrationRecord:
    """One discovered migration file and its SHA-256 checksum."""

    version: str
    filename: str
    checksum: str
    sql: str = field(default="", repr=False, compare=False)


def default_migrations_dir() -> Path:
    """Return the source-checkout migration directory.

    Runtime callers should use discover_migrations() without a path so installed
    package resources are preferred.
    """
    return Path(__file__).resolve().parent.parent.parent / "db" / "migrations"


def _resource_migrations() -> list[MigrationRecord]:
    """Read migrations embedded below the installed pitwall.db package."""
    directory = files("pitwall.db").joinpath("migrations")
    if not directory.is_dir():
        return []

    records: list[MigrationRecord] = []
    resources = sorted(
        (resource for resource in directory.iterdir() if resource.name.endswith(".sql")),
        key=lambda resource: resource.name,
    )
    for resource in resources:
        data = resource.read_bytes()
        records.append(
            MigrationRecord(
                version=Path(resource.name).stem,
                filename=resource.name,
                checksum=_sha256(data),
                sql=data.decode("utf-8"),
            )
        )
    return records


def discover_migrations(
    migrations_dir: Path | str | None = None,
) -> list[MigrationRecord]:
    """Discover ``*.sql`` files, sort lexically, and compute checksums.

    Parameters
    ----------
    migrations_dir:
        Directory containing ``*.sql`` migration files.  Defaults to
        ``db/migrations/`` relative to the repo root.

    Returns
    -------
    list[MigrationRecord]
        Sorted lexicographically by filename.

    Raises
    ------
    FileNotFoundError
        If *migrations_dir* does not exist.
    """
    if migrations_dir is None:
        packaged = _resource_migrations()
        if packaged:
            return packaged
        directory = default_migrations_dir()
    else:
        directory = Path(migrations_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {directory}")
    records: list[MigrationRecord] = []
    for path in sorted(directory.glob("*.sql")):
        data = path.read_bytes()
        records.append(
            MigrationRecord(
                version=path.stem,
                filename=path.name,
                checksum=_sha256(data),
                sql=data.decode("utf-8"),
            )
        )
    return records


def detect_drift(
    expected: list[MigrationRecord],
    applied: dict[str, str],
) -> list[DriftEntry]:
    """Compare expected migrations against previously-applied checksums.

    Parameters
    ----------
    expected:
        The current on-disk migration records (from :func:`discover_migrations`).
    applied:
        Mapping of ``version → checksum`` for migrations previously recorded
        in the database's ``schema_migrations`` table.

    Returns
    -------
    list[DriftEntry]
        Non-empty when drift is detected.  The caller should reject the
        migration run when this list is non-empty.
    """
    drifts: list[DriftEntry] = []
    for rec in expected:
        if rec.version in applied and applied[rec.version] != rec.checksum:
            drifts.append(
                DriftEntry(
                    version=rec.version,
                    filename=rec.filename,
                    recorded_checksum=applied[rec.version],
                    current_checksum=rec.checksum,
                )
            )
    return drifts


@dataclass(frozen=True)
class DriftEntry:
    """A single migration whose on-disk checksum differs from the recorded one."""

    version: str
    filename: str
    recorded_checksum: str
    current_checksum: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
