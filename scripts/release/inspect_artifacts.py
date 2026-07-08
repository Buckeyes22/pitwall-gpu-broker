"""Enforce an allowlisted wheel/sdist content policy."""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

FORBIDDEN_PARTS = {
    ".git",
    ".remember",
    ".code-intel-eval",
    ".mcp.json",
    ".secrets.baseline",
    ".serena",
    "tests",
    "artifacts",
}
SDIST_ROOT_FILES = {
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "NOTICE",
    "pyproject.toml",
    "PKG-INFO",
    ".gitignore",  # hatchling includes the VCS ignore file in source archives
}


def _safe(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and not (set(path.parts) & FORBIDDEN_PARTS)
        and not any(part.endswith("_EVIDENCE_PITWALL.md") for part in path.parts)
    )


def inspect_wheel(path: Path) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    for name in names:
        if not _safe(name):
            errors.append(f"{path.name}: forbidden or unsafe path {name}")
            continue
        first = PurePosixPath(name).parts[0]
        if first != "pitwall" and not first.endswith(".dist-info"):
            errors.append(f"{path.name}: unexpected top-level path {name}")
    required = {"pitwall/py.typed", "pitwall/db/migrations/0001_capabilities.sql"}
    for name in sorted(required - set(names)):
        errors.append(f"{path.name}: missing {name}")
    sql_count = sum(
        name.startswith("pitwall/db/migrations/") and name.endswith(".sql") for name in names
    )
    if sql_count != len(list((Path("db/migrations")).glob("*.sql"))):
        errors.append(f"{path.name}: packaged migration count {sql_count} does not match source")
    return errors


def inspect_sdist(path: Path) -> list[str]:
    errors: list[str] = []
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
    for member in members:
        if member.issym() or member.islnk():
            errors.append(f"{path.name}: links are not permitted: {member.name}")
        if not _safe(member.name):
            errors.append(f"{path.name}: forbidden or unsafe path {member.name}")
            continue
        parts = PurePosixPath(member.name).parts
        if len(parts) < 2 or member.isdir():
            continue
        relative = PurePosixPath(*parts[1:])
        allowed = relative.parts[0] in {"src", "db"} or str(relative) in SDIST_ROOT_FILES
        if not allowed:
            errors.append(f"{path.name}: unexpected sdist path {relative}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    wheels = sorted(args.directory.glob("*.whl"))
    sdists = sorted(args.directory.glob("*.tar.gz"))
    errors: list[str] = []
    if len(wheels) != 1 or len(sdists) != 1:
        errors.append("exactly one wheel and one sdist are required")
    for artifact in wheels:
        errors.extend(inspect_wheel(artifact))
    for artifact in sdists:
        errors.extend(inspect_sdist(artifact))
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print(f"artifact policy passed: {wheels[0].name}, {sdists[0].name}")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
