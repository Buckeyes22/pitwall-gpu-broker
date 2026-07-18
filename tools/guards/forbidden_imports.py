#!/usr/bin/env python3
"""Pre-commit guard banning forbidden imports and patterns in Pitwall runtime code.

Banned patterns:
  - ``from uio`` / ``import uio`` — legacy-ancestor runtime dependencies must not be lifted into Pitwall
  - ``pipeline_cost`` — the legacy ancestor's envelope-shaped billing table must not leak into Pitwall

Only scans src/pitwall/ (runtime code), not tests/ or spec files.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SRC_DIR = Path("src/pitwall")

_FORBIDDEN: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bfrom\s+uio\b"),
        "legacy import 'from uio …' must not appear in Pitwall runtime code",
    ),
    (
        re.compile(r"\bimport\s+uio\b"),
        "legacy import 'import uio' must not appear in Pitwall runtime code",
    ),
    (
        re.compile(r"pipeline_cost"),
        "legacy pipeline_cost table must not appear in Pitwall runtime code",
    ),
]


def check_file(path: Path) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        return [f"{path}: cannot read: {exc}"]

    errors: list[str] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        for pattern, message in _FORBIDDEN:
            if pattern.search(line):
                errors.append(f"{path}:{lineno}: {message}")
    return errors


def main(argv: list[str] | None = None) -> int:
    if not _SRC_DIR.exists():
        print("src/pitwall/ not found, skipping guard", file=sys.stderr)
        return 0

    all_errors: list[str] = []
    for py_file in _SRC_DIR.rglob("*.py"):
        all_errors.extend(check_file(py_file))

    for err in all_errors:
        print(err, file=sys.stderr)

    return 1 if all_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
