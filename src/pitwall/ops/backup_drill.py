"""Postgres PIT restore drill.

Implements ``run_pit_restore_drill`` using a custom-format ``pg_dump`` and
``pg_restore`` into an isolated temporary database. Every base table discovered
in the source schema is compared by row count and order-independent SHA-256
content digest before the temporary database is dropped and evidence is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import stat
import string
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import asyncpg

from pitwall.db.drill_evidence import persist_drill_evidence, write_drill_json_report
from pitwall.security.redaction import redact_text

log = logging.getLogger("pitwall.ops.backup_drill")

DRILL_TYPE = "postgres_pit_restore"

PITWALL_TABLES = [
    "capabilities",
    "providers",
    "workloads",
    "leases",
    "config_audit",
    "kill_log",
]


class TableCheck:
    """Result of validating a single restored table."""

    def __init__(
        self,
        table: str,
        row_count: int,
        checksum: str,
        errors: list[str] | None = None,
    ) -> None:
        self.table = table
        self.row_count = row_count
        self.checksum = checksum
        self.errors = errors or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "row_count": self.row_count,
            "checksum": self.checksum,
            "errors": self.errors,
        }

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0


class BackupDrillReport:
    """Report from a completed PIT restore drill."""

    def __init__(
        self,
        drill_id: str,
        started_at: datetime,
        completed_at: datetime,
        temp_db_name: str,
        target: str,
        passed: bool,
        checks: list[TableCheck],
        errors: list[str],
        config_audit_id: int | None = None,
    ) -> None:
        self.drill_id = drill_id
        self.started_at = started_at
        self.completed_at = completed_at
        self.temp_db_name = temp_db_name
        self.target = target
        self.passed = passed
        self.checks = checks
        self.errors = errors
        self.config_audit_id = config_audit_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "drill_id": self.drill_id,
            "drill_type": DRILL_TYPE,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "temp_db_name": self.temp_db_name,
            "target": self.target,
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "errors": self.errors,
            "config_audit_id": self.config_audit_id,
        }


def _generate_temp_db_name() -> str:
    """Generate a unique temporary database name."""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"pitwall_restore_{suffix}"


async def _compute_table_checksum(
    conn: asyncpg.Connection, schema: str, table: str
) -> tuple[int, str]:
    """Async wrapper for table checksum computation."""
    row_count = await conn.fetchval(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
    if row_count == 0:
        return 0, hashlib.sha256(b"").hexdigest()

    rows = await conn.fetch(f'SELECT * FROM "{schema}"."{table}"')
    canonical_rows = sorted(json.dumps(dict(row), sort_keys=True, default=str) for row in rows)
    hasher = hashlib.sha256()
    for row_data in canonical_rows:
        hasher.update(hashlib.sha256(row_data.encode()).digest())
    return row_count, hasher.hexdigest()


async def _table_exists(conn: asyncpg.Connection, schema: str, table: str) -> bool:
    """Check if a table exists in the given schema."""
    query = """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = $1 AND table_name = $2
        )
    """
    result = await conn.fetchval(query, schema, table)
    return bool(result)


async def _create_schema_in_temp(conn: asyncpg.Connection, schema: str) -> None:
    """Create the pitwall schema in the temporary database."""
    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')


async def _copy_table_schema(src_conn: Any, dst_conn: Any, schema: str, table: str) -> None:
    """Deprecated test seam retained for compatibility; real drills use pg_restore."""
    del src_conn, dst_conn, schema, table
    raise RuntimeError("table-by-table schema copying is unsupported; use pg_restore")


async def _copy_table_data(src_conn: Any, dst_conn: Any, schema: str, table: str) -> None:
    """Deprecated test seam retained for compatibility; real drills use pg_restore."""
    del src_conn, dst_conn, schema, table
    raise RuntimeError("table-by-table data copying is unsupported; use pg_restore")


def _postgres_process_env(database_url: str) -> dict[str, str]:
    """Build libpq environment without placing credentials in process argv."""
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.hostname is None:
        raise ValueError("database URL must be a valid postgres URL")
    env = os.environ.copy()
    env["PGHOST"] = parsed.hostname
    env["PGPORT"] = str(parsed.port or 5432)
    env["PGDATABASE"] = unquote(parsed.path.lstrip("/")) or "postgres"
    if parsed.username is not None:
        env["PGUSER"] = unquote(parsed.username)
    if parsed.password is not None:
        env["PGPASSWORD"] = unquote(parsed.password)
    return env


def _database_url_with_name(database_url: str, database_name: str) -> str:
    """Replace only the database path, retaining safely encoded credentials."""
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.hostname is None:
        raise ValueError("database URL must be a valid postgres URL")
    username = quote(unquote(parsed.username or ""), safe="")
    password = quote(unquote(parsed.password or ""), safe="")
    credentials = username
    if parsed.password is not None:
        credentials = f"{credentials}:{password}"
    if credentials:
        credentials = f"{credentials}@"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, f"{credentials}{host}{port}", f"/{database_name}", "", ""))


async def _drop_temp_database(database_url: str, temp_db_name: str) -> None:
    """Drop the temporary database."""
    proc = subprocess.run(
        [
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f'DROP DATABASE IF EXISTS "{temp_db_name}"',
        ],
        capture_output=True,
        env=_postgres_process_env(database_url),
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        log.warning(
            "Failed to drop temp database %s: %s",
            temp_db_name,
            redact_text(proc.stderr),
        )


async def _create_temp_database(src_database_url: str, temp_db_name: str) -> str:
    """Create a temporary database from the source database template."""
    proc = subprocess.run(
        [
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f'CREATE DATABASE "{temp_db_name}"',
        ],
        capture_output=True,
        env=_postgres_process_env(src_database_url),
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to create temp database: {redact_text(proc.stderr)}")
    return _database_url_with_name(src_database_url, temp_db_name)


async def _restore_schema_and_data(
    src_database_url: str,
    dst_database_url: str,
    schema: str,
) -> asyncpg.Connection:
    """Dump and restore the complete schema through credential-safe tools."""

    with tempfile.TemporaryDirectory(prefix="pitwall-backup-drill-") as directory:
        if stat.S_IMODE(os.stat(directory).st_mode) != 0o700:
            raise RuntimeError("temporary backup directory permissions are not 0700")
        archive = os.path.join(directory, "schema.dump")
        dump = subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--schema",
                schema,
                "--file",
                archive,
            ],
            capture_output=True,
            env=_postgres_process_env(src_database_url),
            text=True,
            timeout=300,
            check=False,
        )
        if dump.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {redact_text(dump.stderr)}")
        os.chmod(archive, 0o600)
        restore = subprocess.run(
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                unquote(urlsplit(dst_database_url).path.lstrip("/")),
                archive,
            ],
            capture_output=True,
            env=_postgres_process_env(dst_database_url),
            text=True,
            timeout=300,
            check=False,
        )
        if restore.returncode != 0:
            raise RuntimeError(f"pg_restore failed: {redact_text(restore.stderr)}")
    return await asyncpg.connect(dst_database_url)


async def _source_tables(pool: asyncpg.Pool, schema: str) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = $1 AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            schema,
        )
    return [str(row["table_name"]) for row in rows]


async def _compare_source_and_restore(
    pool: asyncpg.Pool,
    dst_conn: asyncpg.Connection,
    schema: str,
    tables: list[str],
) -> list[TableCheck]:
    checks = await _validate_tables(dst_conn, schema, tables)
    by_table = {check.table: check for check in checks}
    async with pool.acquire() as src_conn:
        for table in tables:
            check = by_table[table]
            if not check.passed:
                continue
            source_count, source_checksum = await _compute_table_checksum(src_conn, schema, table)
            if check.row_count != source_count:
                check.errors.append(
                    f"row count mismatch: source={source_count}, restored={check.row_count}"
                )
            if check.checksum != source_checksum:
                check.errors.append("content checksum mismatch")
    return checks


async def _validate_tables(
    conn: asyncpg.Connection,
    schema: str,
    tables: list[str],
) -> list[TableCheck]:
    """Validate all tables exist and compute checksums."""
    checks = []
    for table in tables:
        errors: list[str] = []
        if not await _table_exists(conn, schema, table):
            errors.append(f"Table {schema}.{table} does not exist")
            checks.append(TableCheck(table=table, row_count=0, checksum="", errors=errors))
            continue

        try:
            row_count, checksum = await _compute_table_checksum(conn, schema, table)
            checks.append(TableCheck(table=table, row_count=row_count, checksum=checksum))
        except (
            Exception
        ) as e:  # reason: record checksum failure as drill error, continue remaining tables
            errors.append(f"Error computing checksum: {e}")
            checks.append(TableCheck(table=table, row_count=0, checksum="", errors=errors))

    return checks


def _print_restore_plan(schema: str, target: str, temp_db_name: str) -> None:
    """Print the restore plan for dry-run mode."""
    print("PIT Restore Drill Plan")
    print("=" * 50)
    print(f"Target backup: {target}")
    print(f"Schema: {schema}")
    print(f"Temp database: {temp_db_name}")
    print("Core tables expected (the real run validates every discovered base table):")
    for table in PITWALL_TABLES:
        print(f"  - {schema}.{table}")
    print("=" * 50)
    print("DRY-RUN: No changes made.")


async def run_pit_restore_drill(
    ctx: dict[str, Any],
    *,
    schema: str = "pitwall",
    target: str = "latest",
    dry_run: bool = False,
) -> BackupDrillReport:
    """Run a PIT restore drill against the configured database.

    Args:
        ctx: Arq context dict containing db_pool and redis keys.
        schema: The schema to restore (default: pitwall).
        target: Label for the backup target being validated (default: latest).
        dry_run: If True, only print the restore plan without executing.

    Returns:
        BackupDrillReport with drill results including table validation checks.
    """
    started_at = datetime.now(UTC)
    drill_id = f"{DRILL_TYPE}-{started_at.strftime('%Y%m%d-%H%M%S')}"
    temp_db_name = _generate_temp_db_name()

    source_db_url = os.environ.get("PITWALL_DATABASE_URL") or os.environ.get("DATABASE_URL", "")

    if not source_db_url:
        raise ValueError("PITWALL_DATABASE_URL or DATABASE_URL is required")

    if dry_run:
        _print_restore_plan(schema, target, temp_db_name)
        return BackupDrillReport(
            drill_id=drill_id,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            temp_db_name=temp_db_name,
            target=target,
            passed=True,
            checks=[],
            errors=[],
        )

    pool: asyncpg.Pool | None = ctx.get("db_pool")
    if pool is None:
        raise ValueError("db_pool not found in Arq context")

    errors: list[str] = []
    checks: list[TableCheck] = []
    dst_conn: asyncpg.Connection | None = None
    temp_db_url: str | None = None

    try:
        temp_db_url = await _create_temp_database(source_db_url, temp_db_name)
        log.info("Created temp database: %s", temp_db_name)

        tables = await _source_tables(pool, schema)
        if not tables:
            raise RuntimeError(f"source schema {schema!r} has no tables")
        dst_conn = await _restore_schema_and_data(source_db_url, temp_db_url, schema)
        log.info("Restored schema and data to temp database")

        checks = await _compare_source_and_restore(pool, dst_conn, schema, tables)
        log.info("Validated %d tables in temp database", len(checks))

        all_passed = all(c.passed for c in checks)
        if not all_passed:
            failed = [c.table for c in checks if not c.passed]
            errors.append(f"Validation failed for tables: {', '.join(failed)}")

    except (
        Exception
    ) as e:  # reason: drill converts any failure into report errors, never crashes the job
        errors.append(f"Drill failed: {redact_text(e, secrets=(source_db_url,))}")
        log.exception("PIT restore drill failed")
    finally:
        if dst_conn is not None:
            await dst_conn.close()

        if temp_db_url:
            try:
                await _drop_temp_database(source_db_url, temp_db_name)
                log.info("Dropped temp database: %s", temp_db_name)
            except Exception as e:  # reason: temp DB drop is cleanup; failure only logged
                log.warning("Failed to drop temp database %s: %s", temp_db_name, e)

    completed_at = datetime.now(UTC)
    passed = len(errors) == 0 and bool(checks) and all(check.passed for check in checks)

    evidence = {
        "drill_id": drill_id,
        "drill_type": DRILL_TYPE,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "temp_db_name": temp_db_name,
        "target": target,
        "passed": passed,
        "checks": [c.to_dict() for c in checks],
        "errors": errors,
    }

    try:
        config_audit_id = await persist_drill_evidence(
            pool,
            drill_id=drill_id,
            drill_type=DRILL_TYPE,
            evidence=evidence,
            actor="system",
            change_reason=f"weekly scheduled PIT restore validation ({target})",
        )
        log.info("Persisted drill evidence to config_audit: id=%d", config_audit_id)
    except Exception as e:  # reason: evidence persistence failure must not fail the drill
        log.warning("Failed to persist drill evidence: %s", e)
        config_audit_id = None

    try:
        write_drill_json_report(evidence, drill_type=DRILL_TYPE)
        log.info("Wrote drill JSON report to disk")
    except Exception as e:  # reason: report write failure must not fail the drill
        log.warning("Failed to write drill JSON report: %s", e)

    return BackupDrillReport(
        drill_id=drill_id,
        started_at=started_at,
        completed_at=completed_at,
        temp_db_name=temp_db_name,
        target=target,
        passed=passed,
        checks=checks,
        errors=errors,
        config_audit_id=config_audit_id,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the backup drill."""
    parser = argparse.ArgumentParser(description="PIT Restore Drill for Postgres")
    parser.add_argument(
        "--schema",
        default="pitwall",
        help="Schema to restore (default: pitwall)",
    )
    parser.add_argument(
        "--target",
        default="latest",
        help="Backup target label (default: latest)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print restore plan without executing",
    )

    args = parser.parse_args(argv)

    if args.dry_run:
        temp_db_name = _generate_temp_db_name()
        _print_restore_plan(args.schema, args.target, temp_db_name)
        return 0

    print("Starting PIT restore drill...", file=sys.stderr)

    async def _run() -> int:
        pool = await asyncpg.create_pool(
            dsn=os.environ.get("PITWALL_DATABASE_URL") or os.environ.get("DATABASE_URL", ""),
            min_size=1,
            max_size=4,
        )
        ctx = {"db_pool": pool}
        try:
            report = await run_pit_restore_drill(
                ctx,
                schema=args.schema,
                target=args.target,
            )
            print(f"Drill completed: {'PASSED' if report.passed else 'FAILED'}")
            print(f"Drill ID: {report.drill_id}")
            print(f"Config audit ID: {report.config_audit_id}")
            for check in report.checks:
                status = "OK" if check.passed else "FAILED"
                print(
                    f"  [{status}] {check.table}: {check.row_count} rows, checksum={check.checksum[:16]}..."
                )
            if report.errors:
                print("Errors:")
                for error in report.errors:
                    print(f"  - {error}")
            return 0 if report.passed else 1
        finally:
            await pool.close()

    import asyncio

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
