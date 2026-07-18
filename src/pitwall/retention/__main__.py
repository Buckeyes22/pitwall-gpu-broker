"""Operator CLI for bounded retention runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import asyncpg

from pitwall.retention.archive import archive_workloads_to_jsonl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pitwall-gpu-broker retention")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="archive a bounded terminal-workload batch")
    run.add_argument("--archive-dir", type=Path, required=True)
    run.add_argument("--days", type=int, default=90)
    run.add_argument("--batch-size", type=int, default=1_000)
    run.add_argument("--purge", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    async def execute() -> int:
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            parser.error("DATABASE_URL is required")
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            manifest = await archive_workloads_to_jsonl(
                pool,
                args.archive_dir,
                older_than_days=args.days,
                batch_size=args.batch_size,
                purge=args.purge,
                dry_run=args.dry_run,
            )
            print(json.dumps(manifest, sort_keys=True))
            return 0
        finally:
            await pool.close()

    return asyncio.run(execute())


if __name__ == "__main__":
    raise SystemExit(main())
