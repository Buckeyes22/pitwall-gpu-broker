"""Entry-point for ``python -m pitwall.api``."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("PITWALL_API_HOST", "127.0.0.1")
    port = int(os.environ.get("PITWALL_API_PORT", "8080"))
    concurrency = int(os.environ.get("PITWALL_API_MAX_CONCURRENCY", "100"))
    if concurrency < 1:
        raise SystemExit("PITWALL_API_MAX_CONCURRENCY must be at least 1")
    uvicorn.run(
        "pitwall.api.app:app",
        host=host,
        port=port,
        limit_concurrency=concurrency,
    )


if __name__ == "__main__":
    main()
