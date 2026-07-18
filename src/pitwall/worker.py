"""Unsupported legacy GPU-worker entry point.

The incomplete in-repository vLLM worker was removed from the public-alpha
surface by ADR 0002.  Keeping this module as a fail-closed tombstone gives old
automation an actionable error instead of allowing a process to print a
configuration and exit successfully without consuming work.
"""

from __future__ import annotations

import sys

EX_UNAVAILABLE = 69
_MESSAGE = (
    "pitwall.worker is unavailable in the public alpha: the incomplete GPU worker "
    "was deferred by docs/decisions/0002-worker-deferred.md. Configure an existing "
    "RunPod endpoint or a reviewed external image instead."
)


def main(argv: list[str] | None = None) -> int:
    del argv
    print(_MESSAGE, file=sys.stderr)
    return EX_UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
