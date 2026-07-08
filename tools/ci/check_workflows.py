"""Static policy checks for GitHub Actions trust boundaries."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
IMMUTABLE_ACTION = re.compile(r"^\s*uses:\s+([^\s#]+)(?:\s+#.*)?$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def check_workflow(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    if "pull_request_target:" in text:
        errors.append("pull_request_target is forbidden")
    if "permissions:" not in text:
        errors.append("explicit permissions are required")
    if "concurrency:" not in text:
        errors.append("a concurrency policy is required")
    for line_number, line in enumerate(text.splitlines(), 1):
        match = IMMUTABLE_ACTION.match(line)
        if match is None:
            continue
        reference = match.group(1)
        if reference.startswith("./"):
            continue
        if "@" not in reference or not SHA.fullmatch(reference.rsplit("@", 1)[1]):
            errors.append(f"line {line_number}: action is not pinned to a 40-character SHA")
    if path.name == "release.yml":
        if "types: [published]" in text or re.search(r"^\s+release:\s*$", text, re.M):
            errors.append("release publication must have only the immutable tag trigger")
        if "PITWALL_RELEASE_ENABLED" not in text:
            errors.append("production publication needs the explicit repository enable gate")
    return errors


def main() -> int:
    failures: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        failures.extend(f"{path.relative_to(ROOT)}: {error}" for error in check_workflow(path))
    for failure in failures:
        print(failure, file=sys.stderr)
    if not failures:
        print("workflow policy passed")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
