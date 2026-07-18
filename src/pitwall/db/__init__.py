"""Pitwall database CLI and asyncpg connection pool management.

CLI commands::

    pitwall-gpu-broker db migrate   Apply pending migrations
    pitwall-gpu-broker db reset     Drop the pitwall schema (destructive)
    pitwall-gpu-broker db status    Show applied and pending migrations

Pool management::

    from pitwall.db import get_pool, close_pool, db_lifespan, get_db_pool

"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import parse_qs, unquote, urlparse

import asyncpg
from fastapi import Depends, Request

from pitwall.cli_output import Output
from pitwall.migrations import detect_drift, discover_migrations

if TYPE_CHECKING:
    from fastapi import FastAPI


_pool: asyncpg.Pool | None = None


async def get_pool(
    dsn: str | None = None, *, min_size: int = 2, max_size: int = 10
) -> asyncpg.Pool:
    """Return a singleton asyncpg pool configured for PgBouncer transaction mode.

    The pool is created on the first call and reused for all subsequent calls.
    Uses ``statement_cache_size=0`` to avoid prepared statement issues with
    PgBouncer in transaction mode.
    """
    global _pool
    if _pool is None:
        if dsn is None:
            dsn = os.environ.get("DATABASE_URL")
        assert dsn, "dsn or DATABASE_URL environment variable is required"
        _pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
            statement_cache_size=0,
            init=_register_codecs,
        )
    return _pool


async def close_pool() -> None:
    """Close the singleton pool if it has been initialized."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _encode_jsonb(value: object) -> str:
    """Return the text Postgres parses as jsonb.

    Callers pass either a Python object (dict/list) or already-serialized JSON
    text (``model_dump_json()`` / ``json.dumps(...)``). ``json.dumps`` on an
    already-serialized string would double-encode it into a JSON *scalar*
    (``"{\\"k\\":1}"`` instead of ``{"k":1}``), which breaks SQL ``->>``
    introspection (e.g. the leases_active_readiness_signals CHECK) and makes the
    ``isinstance(..., dict)`` decoders fall back to None. Pass strings through
    untouched; serialize everything else.
    """
    return value if isinstance(value, str) else json.dumps(value)


async def _register_codecs(conn: asyncpg.Connection) -> None:
    """Register JSONB codec for dict/list round-trip compatibility."""
    await conn.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=_encode_jsonb,
        decoder=lambda value: json.loads(value),
        format="text",
    )


@asynccontextmanager
async def db_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan context manager that sets up and tears down the DB pool.

    On startup: creates the pool and attaches it to ``app.state.pool`` — unless a
    pool was already injected before startup (e.g. a test fake), in which case it
    is honored as-is and left untouched on shutdown (the injector owns it). This
    keeps hermetic suites (schemathesis fuzz) from opening a real connection.
    On shutdown: closes the pool it created.
    """
    if getattr(app.state, "pool", None) is not None:
        yield
        return
    pool = await get_pool()
    app.state.pool = pool
    yield
    await close_pool()


async def get_db_pool(request: Request) -> asyncpg.Pool:
    """FastAPI dependency that returns the database pool from ``app.state``.

    Raises:
        RuntimeError: If ``app.state.pool`` is not configured.
    """
    pool: asyncpg.Pool | None = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError("Database pool not initialized. Did you forget to use db_lifespan?")
    return pool


DbPoolDep = Annotated[asyncpg.Pool, Depends(get_db_pool)]

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEST_POSTGRES_CONTAINER = "pitwall-test-postgres"
_DESTRUCTIVE_RESET_ENV_VAR = "PITWALL_ALLOW_DESTRUCTIVE_RESET"
_LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}
_MIGRATION_LOCK_ID = 5_780_473_640_160_951_153

_CREATE_MIGRATIONS_TABLE = (
    "CREATE TABLE IF NOT EXISTS pitwall.schema_migrations ("
    " version TEXT PRIMARY KEY,"
    " filename TEXT NOT NULL,"
    " checksum TEXT NOT NULL,"
    " applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    ");"
)
_RECORD_MIGRATION_SQL = """
INSERT INTO pitwall.schema_migrations (version, filename, checksum)
VALUES ($1, $2, $3)
ON CONFLICT (version) DO UPDATE SET
    filename = EXCLUDED.filename,
    checksum = EXCLUDED.checksum,
    applied_at = now();
"""


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        raise SystemExit(1)
    return url


def _allow_destructive_reset_override() -> bool:
    return os.environ.get(_DESTRUCTIVE_RESET_ENV_VAR) == "1"


def _database_host_label(database_url: str) -> str:
    parsed = urlparse(database_url)
    parsed_host = parsed.hostname or ""
    query_hosts = parse_qs(parsed.query).get("host", [])
    if parsed_host and not _is_local_database_host(parsed_host):
        return unquote(parsed_host)
    for host in query_hosts:
        if not _is_local_database_host(host):
            return host
    if query_hosts:
        return query_hosts[0]
    return unquote(parsed_host or "local socket")


def _is_local_database_host(host: str) -> bool:
    normalized = unquote(host).strip().lower().rstrip(".")
    return not normalized or normalized.startswith("/") or normalized in _LOCAL_DATABASE_HOSTS


def _is_local_database_url(database_url: str) -> bool:
    parsed = urlparse(database_url)
    if not _is_local_database_host(parsed.hostname or ""):
        return False
    query_hosts = parse_qs(parsed.query).get("host", [])
    if query_hosts:
        return all(_is_local_database_host(host) for host in query_hosts)
    return True


def _reset_guard_error(database_url: str, *, force: bool) -> str | None:
    if _allow_destructive_reset_override():
        return None
    if not force:
        return (
            "Refusing destructive database reset. Re-run with --force for a local "
            f"database, or set {_DESTRUCTIVE_RESET_ENV_VAR}=1 for an explicit override."
        )
    if not _is_local_database_url(database_url):
        return (
            "Refusing destructive database reset for non-local database host "
            f"{_database_host_label(database_url)!r}. Use a local DATABASE_URL or set "
            f"{_DESTRUCTIVE_RESET_ENV_VAR}=1 to override."
        )
    return None


def _psql_available(database_url: str) -> str | None:
    psql = shutil.which("psql")
    if psql is None:
        return None
    probe = subprocess.run(
        [
            psql,
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            "SELECT 'pitwall_psql_probe';",
        ],
        capture_output=True,
        text=True,
        env=_libpq_env(database_url),
        timeout=10,
        check=False,
    )
    if probe.returncode == 0 and "pitwall_psql_probe" in probe.stdout:
        return psql
    return None


def _libpq_env(database_url: str) -> dict[str, str]:
    """Translate a PostgreSQL URL into libpq environment variables.

    This keeps the URL and password out of process arguments where they would
    be visible to other local users through process listings.
    """

    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use the postgres or postgresql scheme")
    result = os.environ.copy()
    if parsed.hostname:
        result["PGHOST"] = unquote(parsed.hostname)
    if parsed.port:
        result["PGPORT"] = str(parsed.port)
    if parsed.username:
        result["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        result["PGPASSWORD"] = unquote(parsed.password)
    if parsed.path and parsed.path != "/":
        result["PGDATABASE"] = unquote(parsed.path.removeprefix("/"))
    query = parse_qs(parsed.query)
    query_to_env = {
        "host": "PGHOST",
        "port": "PGPORT",
        "user": "PGUSER",
        "dbname": "PGDATABASE",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
    }
    for key, env_name in query_to_env.items():
        if values := query.get(key):
            result[env_name] = unquote(values[-1])
    return result


def _docker_psql(database_url: str) -> list[str] | None:
    docker = shutil.which("docker")
    if docker is None:
        return None
    running = subprocess.run(
        [docker, "inspect", "-f", "{{.State.Running}}", _TEST_POSTGRES_CONTAINER],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if running.returncode != 0 or running.stdout.strip() != "true":
        return None
    parsed = urlparse(database_url)
    user = parsed.username or "pitwall"
    database = parsed.path.removeprefix("/") or "pitwall_test"
    return [
        docker,
        "exec",
        "-i",
        _TEST_POSTGRES_CONTAINER,
        "psql",
        "-U",
        user,
        "-d",
        database,
    ]


def _run_sql(
    database_url: str, sql: str, *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    psql = _psql_available(database_url)
    if psql is not None:
        return subprocess.run(
            [psql, "-v", "ON_ERROR_STOP=1"],
            input=sql,
            cwd=cwd or _REPO_ROOT,
            capture_output=True,
            text=True,
            env=_libpq_env(database_url),
            timeout=60,
            check=False,
        )
    docker_cmd = _docker_psql(database_url)
    if docker_cmd is not None:
        return subprocess.run(
            docker_cmd + ["-v", "ON_ERROR_STOP=1"],
            input=sql,
            cwd=cwd or _REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    print(
        "No psql available and test Postgres container not running. "
        "Install psql or start the Docker container.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _applied_migrations(database_url: str) -> dict[str, str]:
    sql = "SELECT version, checksum FROM pitwall.schema_migrations ORDER BY version;"
    result = _run_sql(database_url, sql)
    if result.returncode != 0:
        return {}
    applied: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) == 2:
            applied[parts[0].strip()] = parts[1].strip()
    return applied


async def _applied_migrations_async(pool: asyncpg.Pool) -> dict[str, str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT version, checksum FROM pitwall.schema_migrations ORDER BY version;"
        )
    return {row["version"]: row["checksum"] for row in rows}


async def _cmd_migrate_async(database_url: str, out: Output) -> int:
    migrations = discover_migrations()
    if not migrations:
        out.print("No migrations found.")
        out.emit()
        return 0

    ensure_sql = "CREATE SCHEMA IF NOT EXISTS pitwall;\n" + _CREATE_MIGRATIONS_TABLE
    pool = await get_pool(database_url, min_size=1, max_size=1)
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock($1);", _MIGRATION_LOCK_ID)
            try:
                try:
                    await conn.execute(ensure_sql)
                except (
                    Exception
                ) as exc:  # reason: migration CLI boundary: print DB error and exit nonzero
                    out.print_error(f"Failed to ensure schema_migrations table:\n{exc}")
                    out.emit()
                    return 1
                applied_rows = await conn.fetch(
                    "SELECT version, checksum FROM pitwall.schema_migrations ORDER BY version;"
                )
                applied = {row["version"]: row["checksum"] for row in applied_rows}
                drifts = detect_drift(migrations, applied)
                if drifts:
                    names = ", ".join(entry.filename for entry in drifts)
                    out.print_error(
                        f"Refusing to migrate because applied migration checksums changed: {names}"
                    )
                    out.emit()
                    return 1

                pending = [m for m in migrations if m.version not in applied]
                if not pending:
                    out.print_success(f"All {len(migrations)} migrations already applied.")
                    out.emit()
                    return 0

                rows: list[list[Any]] = []
                for migration in pending:
                    sql = migration.sql
                    if not sql:
                        raise RuntimeError(
                            f"migration resource {migration.filename!r} did not contain SQL"
                        )
                    try:
                        async with conn.transaction():
                            await conn.execute(sql)
                            await conn.execute(
                                _RECORD_MIGRATION_SQL,
                                migration.version,
                                migration.filename,
                                migration.checksum,
                            )
                    except (
                        Exception
                    ) as exc:  # reason: migration CLI boundary: print DB error and exit nonzero
                        out.print_error(f"Failed to apply {migration.filename}:\n{exc}")
                        out.emit()
                        return 1
                    out.print(f"  applied {migration.filename}")
                    rows.append([migration.filename])
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1);", _MIGRATION_LOCK_ID)

        if rows:
            out.print_table("Applied Migrations", ["filename"], rows)
        out.print_success(f"Applied {len(pending)} migration(s).")
        out.add_json("applied", [m.filename for m in pending])
        out.emit()
        return 0
    finally:
        await close_pool()


def cmd_migrate(*, json_mode: bool = False) -> int:
    out = Output(json_mode)
    database_url = _database_url()
    try:
        return asyncio.run(_cmd_migrate_async(database_url, out))
    except (
        Exception
    ) as exc:  # reason: CLI boundary: any command failure becomes printed error + exit 1
        out.print_error(
            "Failed to run migrations through asyncpg. "
            f"Check DATABASE_URL and migration SQL compatibility:\n{exc}"
        )
        out.emit()
        return 1


def cmd_reset(*, json_mode: bool = False, force: bool = False) -> int:
    out = Output(json_mode)
    database_url = _database_url()
    guard_error = _reset_guard_error(database_url, force=force)
    if guard_error is not None:
        out.print_error(guard_error)
        out.emit()
        return 1
    sql = "DROP SCHEMA IF EXISTS pitwall CASCADE;"
    result = _run_sql(database_url, sql)
    if result.returncode != 0:
        out.print_error(f"Reset failed:\n{result.stderr}")
        out.emit()
        return 1
    out.print_success("Dropped pitwall schema.")
    out.add_json("status", "dropped")
    out.emit()
    return 0


def cmd_status(*, json_mode: bool = False) -> int:
    out = Output(json_mode)
    database_url = _database_url()
    migrations = discover_migrations()
    if not migrations:
        out.print("No migrations found.")
        out.emit()
        return 0

    result = _run_sql(database_url, _CREATE_MIGRATIONS_TABLE)
    if result.returncode != 0:
        out.print_error(
            "schema_migrations table does not exist yet. Run 'pitwall-gpu-broker db migrate' first."
        )
        out.emit()
        return 1

    applied = _applied_migrations(database_url)
    applied_count = 0
    pending_count = 0
    rows: list[list[Any]] = []
    for m in migrations:
        if m.version in applied:
            applied_count += 1
            rows.append([m.filename, "applied"])
        else:
            pending_count += 1
            rows.append([m.filename, "pending"])

    if rows:
        out.print_table("Migrations", ["filename", "status"], rows)
    out.print_success(f"{applied_count} applied, {pending_count} pending, {len(migrations)} total.")
    out.add_json("applied_count", applied_count)
    out.add_json("pending_count", pending_count)
    out.add_json("total", len(migrations))
    out.emit()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    json_flag = "--json" in args
    if json_flag:
        args = [a for a in args if a != "--json"]
    if not args:
        _usage()
        return 1
    command = args[0]
    reset_force = False
    if command == "reset" and "--force" in args[1:]:
        reset_force = True
        args = [command, *(a for a in args[1:] if a != "--force")]
    dispatch = {
        "migrate": lambda: cmd_migrate(json_mode=json_flag),
        "reset": lambda: cmd_reset(json_mode=json_flag, force=reset_force),
        "status": lambda: cmd_status(json_mode=json_flag),
    }
    handler = dispatch.get(command)
    if handler is None:
        print(f"Unknown command: {command}", file=sys.stderr)
        _usage()
        return 1
    return handler()


def _usage() -> None:
    print("Usage: pitwall-gpu-broker db {migrate|reset|status}", file=sys.stderr)


__all__ = [
    "cmd_migrate",
    "cmd_reset",
    "cmd_status",
    "main",
    "get_pool",
    "close_pool",
    "db_lifespan",
    "get_db_pool",
    "DbPoolDep",
]
