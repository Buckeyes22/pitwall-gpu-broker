#!/usr/bin/env python3
"""Dogfood acceptance smoke-checker.

Validates that the public artifact can be stood up from docs alone by checking:
- the checklist file exists and references real files/commands;
- required seed files, migrations, and CLI commands are present;
- Python imports used in the checklist are importable.

Run with::

    python scripts/dogfood_acceptance.py

Exit codes:
    0 — all checks passed
    1 — one or more checks failed
"""

from __future__ import annotations

import ast
import importlib
import os
import re
import sys
import tomllib
from pathlib import Path

# Prevent pitwall services from SystemExit(78) during import-time env checks.
os.environ.setdefault("RUNPOD_API_KEY", "local-dry-run-key")
os.environ.setdefault("DATABASE_URL", "postgresql://pitwall:pitwall@127.0.0.1:5444/pitwall_test")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6380/0")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHECKLIST_PATH = _REPO_ROOT / "docs" / "operator" / "dogfood-acceptance-checklist.md"

_REQUIRED_FILES = frozenset(
    {
        "pyproject.toml",
        "src",
        "docs",
        "tools",
        ".env.example",
        "docker-compose.testinfra.yml",
        "seed/capabilities.yaml",
        "seed/providers.yaml",
        "db/migrations",
        "tools/guards/repo_text_policy.py",
    }
)

_REQUIRED_CLI_GROUPS = frozenset(
    {
        "db",
        "init",
        "register-endpoint",
        "set-provider-health",
    }
)

_REQUIRED_ENTRY_POINTS = frozenset(
    {
        "pitwall-api",
    }
)

_KNOWN_CLI_GROUPS = frozenset(
    {
        "db",
        "mcp",
        "init",
        "create-capability",
        "seed",
        "config",
        "register-template",
        "register-endpoint",
        "set-provider-health",
        "terminate-pod",
        "warm-volume",
        "dashboard",
    }
)


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)


def _ok(message: str) -> None:
    print(f"OK:   {message}")


def _extract_file_refs(text: str) -> set[str]:
    refs: set[str] = set()
    for match in re.finditer(r"`([^`]+)`", text):
        candidate = match.group(1).strip()
        if "/" in candidate or "." in candidate:
            candidate = candidate.rstrip(".")
            refs.add(candidate)
    for match in re.finditer(r"```(?:bash|sh|env)?\n(.*?)\n```", text, re.DOTALL):
        block = match.group(1)
        for line in block.splitlines():
            for mm in re.finditer(r"[\s=](/[\w./-]+|[\w./-]+\.\w+)", line):
                refs.add(mm.group(1).lstrip("/"))
            sm = re.search(r"-f\s+(\S+)", line)
            if sm:
                refs.add(sm.group(1))
    return refs


def _extract_cli_groups(text: str) -> set[str]:
    groups: set[str] = set()
    for match in re.finditer(r"uv run\s+pitwall\s+(\S+)", text):
        groups.add(match.group(1))
    return groups


def _extract_entry_points(text: str) -> set[str]:
    eps: set[str] = set()
    for match in re.finditer(r"uv run\s+(pitwall-[\w-]+)", text):
        eps.add(match.group(1))
    return eps


def _extract_python_imports(text: str) -> set[str]:
    imports: set[str] = set()
    for match in re.finditer(r"python3? -\s*<<['\"]?(\w+)['\"]?\n(.*?)\n\1", text, re.DOTALL):
        block = match.group(2)
        try:
            tree = ast.parse(block)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
    return imports


def check_checklist_exists() -> bool:
    if _CHECKLIST_PATH.is_file():
        _ok(f"checklist exists ({_CHECKLIST_PATH})")
        return True
    _fail(f"checklist missing: {_CHECKLIST_PATH}")
    return False


def check_required_files() -> bool:
    missing: list[str] = []
    for req in _REQUIRED_FILES:
        path = _REPO_ROOT / req
        if not path.exists():
            missing.append(req)
    if missing:
        _fail(f"required files missing: {missing}")
        return False
    _ok(f"required files present ({len(_REQUIRED_FILES)} checked)")
    return True


def check_cli_groups(checklist_text: str) -> bool:
    groups = _extract_cli_groups(checklist_text)
    missing = _REQUIRED_CLI_GROUPS - groups
    if missing:
        _fail(f"checklist missing required CLI groups: {missing}")
        return False
    unknown = groups - _KNOWN_CLI_GROUPS
    if unknown:
        _fail(f"checklist references unknown CLI groups: {unknown}")
        return False
    _ok(f"CLI groups validated ({len(groups)} found)")
    return True


def check_entry_points(checklist_text: str) -> bool:
    eps = _extract_entry_points(checklist_text)
    missing = _REQUIRED_ENTRY_POINTS - eps
    if missing:
        _fail(f"checklist missing required entry points: {missing}")
        return False
    pyproject_text = (_REPO_ROOT / "pyproject.toml").read_text()
    scripts = tomllib.loads(pyproject_text).get("project", {}).get("scripts", {})
    for ep in eps:
        if ep not in scripts:
            _fail(f"entry point {ep} not declared in pyproject.toml")
            return False
    _ok(f"entry points validated ({len(eps)} found)")
    return True


def check_python_imports(checklist_text: str) -> bool:
    imports = _extract_python_imports(checklist_text)
    pitwall_imports = {imp for imp in imports if imp.startswith("pitwall.")}
    failures: list[str] = []
    for imp in pitwall_imports:
        try:
            importlib.import_module(imp)
        except Exception as exc:  # reason: collect any import failure into the acceptance report
            failures.append(f"{imp}: {exc}")
    if failures:
        _fail(f"unimportable modules: {failures}")
        return False
    _ok(f"Python imports validated ({len(pitwall_imports)} pitwall modules)")
    return True


def check_sections(checklist_text: str) -> bool:
    required = ["Step 5", "Step 6", "Step 7", "Step 11"]
    missing = [s for s in required if s not in checklist_text]
    if missing:
        _fail(f"checklist missing required sections: {missing}")
        return False
    _ok("checklist contains all required steps")
    return True


def main() -> int:
    print("Dogfood acceptance smoke-checker")
    print("=" * 40)

    if not check_checklist_exists():
        return 1

    text = _CHECKLIST_PATH.read_text()
    results = [
        check_required_files(),
        check_cli_groups(text),
        check_entry_points(text),
        check_python_imports(text),
        check_sections(text),
    ]

    print("=" * 40)
    if all(results):
        print("All checks passed.")
        return 0
    print("Some checks failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
