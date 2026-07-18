"""Entry-point for ``python -m pitwall.cost_exporter``."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("PITWALL_COST_EXPORTER_HOST", "127.0.0.1")
    port = int(os.environ.get("PITWALL_COST_EXPORTER_PORT", "9109"))
    concurrency = int(os.environ.get("PITWALL_COST_EXPORTER_MAX_CONCURRENCY", "20"))
    if concurrency < 1:
        raise SystemExit("PITWALL_COST_EXPORTER_MAX_CONCURRENCY must be at least 1")
    uvicorn.run(
        "pitwall.cost_exporter:app",
        host=host,
        port=port,
        limit_concurrency=concurrency,
    )


if __name__ == "__main__":
    main()
