"""Entry-point for ``python -m pitwall.reconciler``.

Supports two modes:
  python -m pitwall.reconciler         run the Arq worker
  python -m pitwall.reconciler check   validate Redis configuration
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from pitwall.reconciler import (
    _ARQ_AVAILABLE,
    WorkerSettings,
    check_redis_config,
)

if TYPE_CHECKING:
    from arq.worker import Worker

create_worker: Callable[..., Worker] | None
try:
    from arq.worker import create_worker as _arq_create_worker

    create_worker = _arq_create_worker
except ImportError:
    create_worker = None


def main() -> None:
    args = sys.argv[1:] if len(sys.argv) > 1 else []
    if args and args[0] == "check":
        raise SystemExit(check_redis_config())
    if not _ARQ_AVAILABLE or create_worker is None:
        print(
            "arq is not installed; cannot run worker. "
            "Install arq or run 'python -m pitwall.reconciler check' to validate config.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    worker = create_worker(WorkerSettings)
    worker.run()


if __name__ == "__main__":
    main()
