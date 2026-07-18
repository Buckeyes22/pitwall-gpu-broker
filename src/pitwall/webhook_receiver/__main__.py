"""Entry-point for ``python -m pitwall.webhook_receiver``."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("PITWALL_WEBHOOK_HOST", "127.0.0.1")
    port = int(os.environ.get("PITWALL_WEBHOOK_RECEIVER_PORT", "8082"))
    concurrency = int(os.environ.get("PITWALL_WEBHOOK_MAX_CONCURRENCY", "50"))
    if concurrency < 1:
        raise SystemExit("PITWALL_WEBHOOK_MAX_CONCURRENCY must be at least 1")
    uvicorn.run(
        "pitwall.webhook_receiver:app",
        host=host,
        port=port,
        limit_concurrency=concurrency,
    )


if __name__ == "__main__":
    main()
