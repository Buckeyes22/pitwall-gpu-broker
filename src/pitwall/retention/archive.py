"""Encrypted, bounded, auditable workload archive and purge lifecycle."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import asyncpg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ARCHIVE_RETENTION_DAYS = 90
DEFAULT_BATCH_SIZE = 1_000
MAX_BATCH_SIZE = 10_000
MANIFEST_FILENAME = "manifest.json"
ARCHIVE_FORMAT_VERSION = 1
TERMINAL_STATES = ("completed", "failed", "cancelled", "timed_out")
ObjectDelete = Callable[[list[str]], Awaitable[None]]

_SELECT_BATCH = """
SELECT w.*,
       COALESCE((
         SELECT jsonb_agg(to_jsonb(i)) FROM pitwall.idempotency_keys i
         WHERE i.workload_id = w.id
       ), '[]'::jsonb) AS related_idempotency_keys,
       COALESCE((
         SELECT jsonb_agg(to_jsonb(d)) FROM pitwall.runpod_webhook_deliveries d
         WHERE d.runpod_job_id = w.runpod_job_id
       ), '[]'::jsonb) AS related_inbound_webhooks,
       COALESCE((
         SELECT jsonb_agg(to_jsonb(f)) FROM pitwall.webhook_delivery_failures f
         WHERE f.workload_id = w.id
       ), '[]'::jsonb) AS related_outbound_webhook_failures
FROM pitwall.workloads w
WHERE w.submitted_at < $1
  AND w.state = ANY($2::text[])
ORDER BY w.submitted_at, w.id
LIMIT $3
FOR UPDATE OF w SKIP LOCKED
"""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    column_names = row.keys()
    for key in column_names:
        value = row[key]
        if isinstance(value, (dict, list)) or value is None:
            result[key] = value
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


def _decode_key(value: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("archive encryption key must be URL-safe base64") from exc
    if len(key) != 32:
        raise ValueError("archive encryption key must decode to exactly 32 bytes")
    return key


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        os.chmod(temporary, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    path.chmod(0o600)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _object_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, Mapping):
        for name, child in value.items():
            if name in {"r2_key", "object_key", "staging_key"} and isinstance(child, str):
                keys.add(child)
            keys.update(_object_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_object_keys(child))
    return keys


async def _delete_related(
    conn: asyncpg.Connection, rows: list[dict[str, Any]], cutoff: datetime
) -> int:
    workload_ids = [str(row["id"]) for row in rows]
    runpod_job_ids = [str(row["runpod_job_id"]) for row in rows if row.get("runpod_job_id")]
    await conn.execute(
        "DELETE FROM pitwall.idempotency_keys WHERE workload_id = ANY($1::text[])",
        workload_ids,
    )
    if runpod_job_ids:
        await conn.execute(
            "DELETE FROM pitwall.runpod_webhook_deliveries WHERE runpod_job_id = ANY($1::text[])",
            runpod_job_ids,
        )
    await conn.execute(
        "DELETE FROM pitwall.webhook_delivery_failures WHERE workload_id = ANY($1::text[])",
        workload_ids,
    )
    result = await conn.execute(
        "DELETE FROM pitwall.workloads WHERE id = ANY($1::text[]) "
        "AND submitted_at < $2 AND state = ANY($3::text[])",
        workload_ids,
        cutoff,
        list(TERMINAL_STATES),
    )
    return int(result.rsplit(" ", 1)[-1])


async def archive_workloads_to_jsonl(
    pool: asyncpg.Pool,
    output_dir: Path,
    *,
    older_than_days: int = ARCHIVE_RETENTION_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    purge: bool = False,
    dry_run: bool = False,
    encryption_key: str | None = None,
    key_version: str | None = None,
    object_delete: ObjectDelete | None = None,
) -> dict[str, Any]:
    """Archive one bounded terminal-workload batch and optionally purge it.

    Files use AES-256-GCM and mode 0600 under a mode-0700 per-run directory.
    The database deletion happens only after the encrypted file and preliminary
    manifest have been durably written. A failed database commit leaves an
    explicit uncommitted manifest instead of silently claiming a purge.
    """
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    key_text = encryption_key or os.environ.get("PITWALL_ARCHIVE_ENCRYPTION_KEY", "")
    version = key_version or os.environ.get("PITWALL_ARCHIVE_ENCRYPTION_KEY_VERSION", "")
    if not dry_run and (not key_text or not version):
        raise ValueError("archive encryption key and key version are required")

    started_at = datetime.now(UTC)
    cutoff = started_at - timedelta(days=older_than_days)
    run_id = f"ret_{started_at.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}"
    mode = "dry-run" if dry_run else "archive-purge" if purge else "archive"
    rows: list[dict[str, Any]] = []
    deleted_count = 0
    run_directory: Path | None = None
    manifest_path: Path | None = None

    async with pool.acquire() as conn, conn.transaction():
        selected = await conn.fetch(_SELECT_BATCH, cutoff, list(TERMINAL_STATES), batch_size)
        rows = [_row_to_dict(row) for row in selected]
        if dry_run:
            return {
                "format_version": ARCHIVE_FORMAT_VERSION,
                "run_id": run_id,
                "mode": mode,
                "cutoff_at": cutoff.isoformat(),
                "workload_count": len(rows),
                "deleted_count": 0,
                "database_committed": False,
            }

        run_directory = output_dir / run_id
        _secure_directory(run_directory)
        plaintext = b"".join(_canonical_json(row) + b"\n" for row in rows)
        nonce = os.urandom(12)
        aad = f"pitwall-retention:{ARCHIVE_FORMAT_VERSION}:{run_id}:{version}".encode()
        ciphertext = AESGCM(_decode_key(key_text)).encrypt(nonce, plaintext, aad)
        archive_path = run_directory / "workloads.jsonl.enc"
        _atomic_write(archive_path, ciphertext)
        manifest: dict[str, Any] = {
            "format_version": ARCHIVE_FORMAT_VERSION,
            "run_id": run_id,
            "mode": mode,
            "started_at": started_at.isoformat(),
            "cutoff_at": cutoff.isoformat(),
            "workload_count": len(rows),
            "deleted_count": 0,
            "key_version": version,
            "cipher": "AES-256-GCM",
            "nonce_b64": base64.urlsafe_b64encode(nonce).decode("ascii"),
            "aad": aad.decode("ascii"),
            "archive_file": archive_path.name,
            "archive_size_bytes": len(ciphertext),
            "archive_sha256": _sha256_file(archive_path),
            "database_commit_status": "pending",
            "commit_evidence_file": "commit.json",
        }
        manifest_path = run_directory / MANIFEST_FILENAME
        _atomic_write(manifest_path, _canonical_json(manifest))

        if purge and rows:
            external_keys = sorted(_object_keys(rows))
            if external_keys and object_delete is None:
                raise RuntimeError("archive contains object-storage keys but no deletion adapter")
            if external_keys and object_delete is not None:
                await object_delete(external_keys)
            deleted_count = await _delete_related(conn, rows, cutoff)

        completed_at = datetime.now(UTC)
        await conn.execute(
            """
            INSERT INTO pitwall.retention_runs
              (id, started_at, completed_at, cutoff_at, mode, archive_path,
               manifest_sha256, workload_count, deleted_count, key_version, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'completed')
            """,
            run_id,
            started_at,
            completed_at,
            cutoff,
            mode,
            str(run_directory),
            _sha256_file(manifest_path),
            len(rows),
            deleted_count,
            version,
        )
        await conn.execute(
            """
            INSERT INTO pitwall.config_audit
              (entity_type, entity_id, action, old_value, new_value, actor, change_reason)
            VALUES ('retention_run',$1,$2,NULL,$3::jsonb,'system',$4)
            """,
            run_id,
            "purge" if purge else "archive",
            json.dumps({"workload_count": len(rows), "deleted_count": deleted_count}),
            f"bounded {mode} retention run",
        )

    assert run_directory is not None and manifest_path is not None
    completed_manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    completed_manifest["deleted_count"] = deleted_count
    completed_manifest["completed_at"] = datetime.now(UTC).isoformat()
    completed_manifest["database_committed"] = True
    _atomic_write(
        run_directory / "commit.json",
        _canonical_json(
            {
                "run_id": run_id,
                "database_committed": True,
                "manifest_sha256": _sha256_file(manifest_path),
            }
        ),
    )
    return completed_manifest
