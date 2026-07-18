"""Export the deterministic public OpenAPI document without external services."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("RUNPOD_API_KEY", "openapi-export-placeholder")
    os.environ.setdefault("DATABASE_URL", "postgresql://placeholder@127.0.0.1/pitwall")
    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
    os.environ.setdefault("PITWALL_INBOUND_RATE_LIMIT", "off")
    from pitwall.api.app import app

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
