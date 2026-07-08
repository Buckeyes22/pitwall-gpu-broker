#!/usr/bin/env python3
"""Guard worker-owned paths against the deprecated Hugging Face CLI binary."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

_WORKER_OWNED_EXACT_FILES = (Path("src/pitwall/worker.py"),)
_WORKER_OWNED_DIRS = (Path("src/pitwall/workers"),)
_WORKER_DOCKER_PREFIXES = ("Dockerfile.worker-", "worker-", "operator-vllm-")
_EXTENSIONS_TO_SKIP = {
    ".pyc",
    ".pyo",
    ".so",
    ".o",
    ".a",
    ".dylib",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
}
_DEPRECATED_HF_CLI = ("huggingface-cli", "download")
_DEPRECATED_HF_CLI_PATTERN = re.compile(
    rf"(?<![\w-]){re.escape(_DEPRECATED_HF_CLI[0])}\s+"
    rf"{re.escape(_DEPRECATED_HF_CLI[1])}\b"
)


def _deprecated_command() -> str:
    return " ".join(_DEPRECATED_HF_CLI)


def _is_worker_owned_relative_path(relative_path: Path) -> bool:
    if relative_path in _WORKER_OWNED_EXACT_FILES:
        return True
    if relative_path.parent == Path("docker") and relative_path.name.startswith(
        _WORKER_DOCKER_PREFIXES
    ):
        return True
    return any(
        relative_path == directory or directory in relative_path.parents
        for directory in _WORKER_OWNED_DIRS
    )


def _relative_to_root(root: Path, path: Path) -> Path | None:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return None


def _should_scan(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() not in _EXTENSIONS_TO_SKIP


def iter_worker_owned_files(root: Path) -> Iterable[Path]:
    docker_dir = root / "docker"
    if docker_dir.is_dir():
        for path in sorted(docker_dir.iterdir()):
            relative_path = _relative_to_root(root, path)
            if (
                relative_path is not None
                and _is_worker_owned_relative_path(relative_path)
                and _should_scan(path)
            ):
                yield path

    for relative_path in _WORKER_OWNED_EXACT_FILES:
        path = root / relative_path
        if _should_scan(path):
            yield path

    for relative_dir in _WORKER_OWNED_DIRS:
        directory = root / relative_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if _should_scan(path):
                yield path


def iter_requested_worker_files(root: Path, paths: Iterable[str]) -> Iterable[Path]:
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        relative_path = _relative_to_root(root, path)
        if relative_path is None or not _is_worker_owned_relative_path(relative_path):
            continue
        if _should_scan(path):
            yield path


def check_file(root: Path, path: Path) -> list[str]:
    relative_path = _relative_to_root(root, path) or path
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        return [f"{relative_path}: cannot read: {exc}"]

    errors: list[str] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if _DEPRECATED_HF_CLI_PATTERN.search(line):
            errors.append(
                f"{relative_path}:{lineno}: deprecated HF CLI command "
                f"{_deprecated_command()!r}; use 'hf download' instead"
            )
    return errors


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail if worker-owned paths use the deprecated HF CLI binary.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root to scan. Defaults to the current working directory.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Optional pre-commit file list. Non-worker paths are ignored.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    root = args.root.resolve()
    paths = (
        iter_requested_worker_files(root, args.paths)
        if args.paths
        else iter_worker_owned_files(root)
    )

    all_errors: list[str] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        all_errors.extend(check_file(root, path))

    for err in all_errors:
        print(err, file=sys.stderr)

    return 1 if all_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
