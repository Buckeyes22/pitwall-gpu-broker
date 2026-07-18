#!/usr/bin/env python3
"""Guard worker-owned Dockerfiles against unpinned ML packages (L13).

WhisperX-class dep stacks (torch, torchaudio, huggingface-hub, transformers,
whisper/whisperx) need monkey-patches for version churn. To avoid inheriting
patch stacks, these packages MUST be pinned to a specific version (==) in
worker images.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

_WORKER_DOCKERFILE_PATTERN = re.compile(r"^Dockerfile\.worker-")
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

_ML_PACKAGES = (
    "torch",
    "torchaudio",
    "huggingface-hub",
    "huggingface_hub",
    "transformers",
    "whisper",
    "whisperx",
)

_ML_PACKAGE_PATTERN = re.compile(r"\b(" + "|".join(re.escape(p) for p in _ML_PACKAGES) + r")\b")


def _is_worker_owned_dockerfile(path: Path) -> bool:
    return path.parent.name == "docker" and bool(_WORKER_DOCKERFILE_PATTERN.match(path.name))


def _should_scan(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() not in _EXTENSIONS_TO_SKIP


def _iter_worker_dockerfiles(root: Path) -> Iterable[Path]:
    docker_dir = root / "docker"
    if not docker_dir.is_dir():
        return
    for path in sorted(docker_dir.iterdir()):
        if _is_worker_owned_dockerfile(path) and _should_scan(path):
            yield path


def iter_requested_files(root: Path, paths: Iterable[str]) -> Iterable[Path]:
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        if _is_worker_owned_dockerfile(path) and _should_scan(path):
            yield path


def _relative_to_root(root: Path, path: Path) -> Path | None:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return None


def _extract_pip_install_lines(content: str) -> list[tuple[int, str]]:
    lines = content.splitlines()
    results: list[tuple[int, str]] = []
    in_multiline_run = False
    multiline_buffer = ""

    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("RUN"):
            remainder = stripped[3:].strip()
            if "pip install" in remainder or remainder[:3] == "pip":
                if " \\" in remainder or remainder.endswith("\\"):
                    in_multiline_run = True
                    multiline_buffer = remainder
                else:
                    results.append((lineno, remainder))

        elif in_multiline_run:
            multiline_buffer += " " + stripped
            if not stripped.endswith("\\"):
                in_multiline_run = False
                results.append((lineno, multiline_buffer))

    return results


def check_file(root: Path, path: Path) -> list[str]:
    relative_path = _relative_to_root(root, path) or path
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        return [f"{relative_path}: cannot read: {exc}"]

    errors: list[str] = []
    install_lines = _extract_pip_install_lines(content)

    for lineno, install_cmd in install_lines:
        ml_matches = list(_ML_PACKAGE_PATTERN.finditer(install_cmd))
        if not ml_matches:
            continue

        for match in ml_matches:
            pkg_name = match.group(1)
            end = match.end()

            after = install_cmd[end:]
            pin_match = re.match(r"==([\d.]+)", after)
            if pin_match:
                continue

            if re.match(r"(>=?|<=?|~=)", after):
                pin_type = "range constraint"
                errors.append(
                    f"{relative_path}:{lineno}: "
                    f"ML package {pkg_name!r} has {pin_type}; "
                    f"pin to a specific version (==) for L13 compliance"
                )
                continue

            pin_type = "unpinned"
            errors.append(
                f"{relative_path}:{lineno}: "
                f"ML package {pkg_name!r} is {pin_type}; "
                f"pin to a specific version (==) for L13 compliance"
            )

    return errors


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail if worker-owned Dockerfiles have unpinned ML packages.",
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

    if args.paths:
        paths = list(iter_requested_files(root, args.paths))
    else:
        paths = list(_iter_worker_dockerfiles(root))

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
