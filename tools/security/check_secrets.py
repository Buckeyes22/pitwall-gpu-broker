"""Deterministic, repository-wide detect-secrets policy gate.

The upstream CLI rewrites timestamps and line numbers in a baseline, which made
the former CI diff both noisy and scope-dependent. This gate compares semantic
fingerprints instead: every current finding must have an explicit false-positive
decision in the committed baseline, and stale decisions must be removed. Tests
and fixtures are intentionally included.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_BASELINE = _ROOT / ".secrets.baseline"
_EXCLUDES = (
    r"(?:^|/)\.git/",
    r"(?:^|/)\.venv/",
    r"(?:^|/)uv\.lock$",
    r"(?:^|/)\.secrets\.baseline$",
)


def _fingerprints(document: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (filename, str(item["type"]), str(item["hashed_secret"]))
        for filename, items in document.get("results", {}).items()
        for item in items
    }


def _unreviewed(document: dict[str, Any]) -> list[tuple[str, int, str]]:
    return [
        (filename, int(item.get("line_number", 0)), str(item["type"]))
        for filename, items in document.get("results", {}).items()
        for item in items
        if item.get("is_secret") is not False
    ]


def _scan(baseline: Path) -> dict[str, Any]:
    executable = shutil.which("detect-secrets")
    if executable is None:
        raise RuntimeError("detect-secrets is not installed; run `uv sync --frozen --extra dev`")
    command = [executable, "scan", "--no-verify", "--baseline", str(baseline)]
    for pattern in _EXCLUDES:
        command.extend(("--exclude-files", pattern))
    result = subprocess.run(command, cwd=_ROOT, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "detect-secrets scan failed")
    return json.loads(baseline.read_text(encoding="utf-8"))


def main() -> int:
    approved = json.loads(_BASELINE.read_text(encoding="utf-8"))
    pending = _unreviewed(approved)
    if pending:
        for filename, line, kind in pending:
            print(f"unreviewed baseline finding: {filename}:{line}: {kind}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="pitwall-secret-scan-") as temp_dir:
        scan_baseline = Path(temp_dir) / "baseline.json"
        shutil.copy2(_BASELINE, scan_baseline)
        current = _scan(scan_baseline)

    approved_keys = _fingerprints(approved)
    current_keys = _fingerprints(current)
    additions = sorted(current_keys - approved_keys)
    stale = sorted(approved_keys - current_keys)
    for filename, kind, _digest in additions:
        print(f"new potential secret: {filename}: {kind}", file=sys.stderr)
    for filename, kind, _digest in stale:
        print(f"stale secret-baseline entry: {filename}: {kind}", file=sys.stderr)
    if additions or stale:
        print(
            "regenerate with the canonical scan and audit every changed finding",
            file=sys.stderr,
        )
        return 1
    print(f"secret scan passed: {len(current_keys)} reviewed findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
