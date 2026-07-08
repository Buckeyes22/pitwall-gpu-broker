#!/usr/bin/env python3
"""Pre-commit policy guard banning forbidden text across the repo.

The public guard only contains shape-based or public deprecation rules. Private
literal rules belong in the local overlay file, which is intentionally ignored
by git.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

_EPTEST_ID_RE = r"eptest[0-9]{8}"
_LOCAL_POLICY_ENV = "PITWALL_TEXT_POLICY_EXTRA"
_LOCAL_POLICY_RELATIVE_PATH = Path("tools/guards/repo_text_policy.local")
PolicyRule = tuple[re.Pattern[str], str]


def _joined_literal(*parts: str) -> str:
    return "".join(parts)


class LocalPolicyError(ValueError):
    """Raised when a local text-policy overlay cannot be loaded."""


_BANNED: list[PolicyRule] = [
    (
        re.compile(r"huggingface-cli\s+download"),
        f"TP-DEPREC: {_joined_literal('huggingface-cli', ' download')} is deprecated - use 'hf download' instead",
    ),
    (
        re.compile(_joined_literal(r"/r2", r"/tokens", r"\b")),
        "TP-DEPREC: Cloudflare deprecated R2 token-rotation endpoint",
    ),
    (
        re.compile(_joined_literal("r2_", "credentials_", "rotated")),
        "TP-DEPREC: zombie field from dead rotation wiring",
    ),
    (
        re.compile(
            rf"\b(?!{_EPTEST_ID_RE}\.proxy\.runpod\.net)[a-z0-9]{{14}}\.proxy\.runpod\.net\b"
        ),
        "TP-SHAPE: RunPod proxy hostname",
    ),
    (
        re.compile(
            rf"\b(?!{_EPTEST_ID_RE}-\d+\.proxy\.runpod\.net)[a-z0-9]{{14}}-\d+\.proxy\.runpod\.net\b"
        ),
        "TP-SHAPE: RunPod proxy hostname",
    ),
    (
        re.compile(rf"api\.runpod\.ai/v2/(?!{_EPTEST_ID_RE}\b)[a-z0-9]{{10,}}"),
        "TP-SHAPE: RunPod API endpoint URL",
    ),
    (
        re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[0-1]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
        "TP-SHAPE: Tailscale CGNAT address",
    ),
    (
        # Sanctioned contexts: github.com/<owner> and ghcr.io/<owner> URLs, CODEOWNERS
        # @owner entries, and the GitHub Actions fork-guard idiom
        # `github.repository == '<owner>/...'`.
        re.compile(
            r"(?<!github\.com/)(?<!ghcr\.io/)(?<!github\.repository == ')(?<!@)" + "buck" + "eyes22"
        ),
        "TP-OWNER: bare owner handle outside sanctioned URL context",
    ),
]

# No path exemptions: every tracked file must pass. Add entries here only for
# files that must intentionally quote banned strings (none currently exist).
_PATHS_TO_SKIP: tuple[re.Pattern[str], ...] = ()

_EXTENSIONS_TO_SKIP: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".whl",
    ".pyc",
    ".pyo",
    ".so",
    ".o",
    ".a",
    ".dylib",
    ".db",
    ".sqlite",
    ".parquet",
    ".lock",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _local_policy_path() -> Path:
    configured_path = os.environ.get(_LOCAL_POLICY_ENV)
    if configured_path:
        return Path(configured_path).expanduser()
    return _repo_root() / _LOCAL_POLICY_RELATIVE_PATH


def _load_local_policy_rules() -> list[PolicyRule]:
    path = _local_policy_path()
    if not path.exists():
        return []

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LocalPolicyError(f"{path}: cannot read local text policy overlay: {exc}") from exc

    rules: list[PolicyRule] = []
    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        try:
            pattern = re.compile(line)
        except re.error as exc:
            raise LocalPolicyError(
                f"{path}:{lineno}: invalid local text policy regex: {exc}"
            ) from exc
        rules.append((pattern, "LOCAL: private pattern"))
    return rules


def should_skip_path(path: Path) -> bool:
    path_text = path.as_posix()
    return any(pattern.search(path_text) for pattern in _PATHS_TO_SKIP)


def check_file(path: Path, rules: Sequence[PolicyRule] = _BANNED) -> list[str]:
    if should_skip_path(path):
        return []

    if path.suffix.lower() in _EXTENSIONS_TO_SKIP:
        return []

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        return [f"{path}: cannot read: {exc}"]

    errors: list[str] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        for pattern, message in rules:
            if pattern.search(line):
                errors.append(f"{path}:{lineno}: {message}")
    return errors


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python tools/guards/repo_text_policy.py FILE [FILE …]", file=sys.stderr)
        return 2

    try:
        rules = [*_BANNED, *_load_local_policy_rules()]
    except LocalPolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    all_errors: list[str] = []
    for filepath in args:
        all_errors.extend(check_file(Path(filepath), rules))

    for err in all_errors:
        print(err, file=sys.stderr)

    return 1 if all_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
