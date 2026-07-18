"""Validate the immutable source identity used for a release candidate."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def validate(tag: str, *, allow_dirty: bool = False) -> list[str]:
    errors: list[str] = []
    version = _version()
    if tag != f"v{version}":
        errors.append(f"tag {tag!r} does not match project version v{version}")
    if not re.fullmatch(r"v0\.[1-9][0-9]*\.[0-9]+(?:a[1-9][0-9]*)?", tag):
        errors.append("the public alpha tag must be pre-1.0 and match v0.MINOR.PATCH[aN]")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(
        rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$", changelog, re.M
    )
    if match is None:
        errors.append(f"CHANGELOG.md needs a dated '## [{version}] - YYYY-MM-DD' entry")
    else:
        release_date = date.fromisoformat(match.group(1))
        if release_date > date.today():
            errors.append("changelog release date cannot be in the future")

    release_notes = ROOT / "docs" / "releases" / f"{tag}.md"
    if not release_notes.is_file() or not release_notes.read_text(encoding="utf-8").strip():
        errors.append(f"{release_notes.relative_to(ROOT)} must contain release notes")

    if not allow_dirty:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if status:
            errors.append("release source tree is dirty or contains untracked files")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    errors = validate(args.tag, allow_dirty=args.allow_dirty)
    for error in errors:
        print(f"release validation failed: {error}", file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
