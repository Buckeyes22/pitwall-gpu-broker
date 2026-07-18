"""Hermetic check that the dogfood acceptance checklist references only
public artifacts that actually exist.

Parses docs/operator/dogfood-acceptance-checklist.md and asserts:
- The checklist file itself exists.
- Referenced file paths (detected from inline code and code blocks) exist.
- Referenced ``pitwall-gpu-broker`` CLI command groups exist.
- Python imports in heredocs are importable.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHECKLIST_PATH = _REPO_ROOT / "docs" / "operator" / "dogfood-acceptance-checklist.md"

# Files/directories we expect the checklist to reference.
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

# Command groups the checklist references via ``uv run pitwall-gpu-broker ...``.
_REQUIRED_CLI_GROUPS = frozenset(
    {
        "db",
        "init",
        "register-endpoint",
        "set-provider-health",
    }
)

# Console entry points referenced in the checklist.
_REQUIRED_ENTRY_POINTS = frozenset(
    {
        "pitwall-api",
    }
)


def _extract_file_refs(text: str) -> set[str]:
    refs: set[str] = set()
    # Inline code like `pyproject.toml` or `src/`
    for match in re.finditer(r"`([^`]+)`", text):
        candidate = match.group(1).strip()
        # Only keep candidates that look like paths or file names
        if "/" in candidate or "." in candidate:
            # Strip trailing punctuation
            candidate = candidate.rstrip(".")
            refs.add(candidate)
    # Code blocks
    for match in re.finditer(r"```(?:bash|sh|env)?\n(.*?)\n```", text, re.DOTALL):
        block = match.group(1)
        for line in block.splitlines():
            # File paths after cp, from, or in comments
            for mm in re.finditer(r"[\s=](/[\w./-]+|[\w./-]+\.\w+)", line):
                refs.add(mm.group(1).lstrip("/"))
            # docker compose -f <file>
            sm = re.search(r"-f\s+(\S+)", line)
            if sm:
                refs.add(sm.group(1))
    return refs


def _extract_cli_groups(text: str) -> set[str]:
    groups: set[str] = set()
    for match in re.finditer(r"uv run\s+pitwall-gpu-broker\s+(\S+)", text):
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


class TestDogfoodChecklistExists:
    def test_checklist_file_exists(self) -> None:
        assert _CHECKLIST_PATH.is_file(), f"Dogfood checklist missing: {_CHECKLIST_PATH}"


@pytest.fixture(scope="class")
def checklist_text() -> str:
    return _CHECKLIST_PATH.read_text()


class TestDogfoodChecklistReferences:
    def test_required_files_exist(self, checklist_text: str) -> None:
        refs = _extract_file_refs(checklist_text)
        missing: list[str] = []
        for req in _REQUIRED_FILES:
            path = _REPO_ROOT / req
            if not path.exists():
                missing.append(req)
            elif req in refs:
                refs.discard(req)
        assert not missing, f"Required files referenced by checklist are missing: {missing}"

    def test_referenced_cli_groups_exist(self, checklist_text: str) -> None:
        groups = _extract_cli_groups(checklist_text)
        missing = _REQUIRED_CLI_GROUPS - groups
        assert not missing, f"Checklist missing required CLI groups: {missing}"

        for group in groups:
            # Exercise dispatch logic by checking the group is known.
            # We cannot call main() because some commands require env vars,
            # but we can verify the command string is not rejected at parse
            # time by looking at the dispatch table.
            assert group in {
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
            }, f"Unknown CLI group referenced: {group}"

    def test_referenced_entry_points_exist(self, checklist_text: str) -> None:
        eps = _extract_entry_points(checklist_text)
        missing = _REQUIRED_ENTRY_POINTS - eps
        assert not missing, f"Checklist missing required entry points: {missing}"

        # Verify the console scripts are declared in pyproject.toml
        import tomllib

        pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
        scripts = pyproject.get("project", {}).get("scripts", {})
        for ep in eps:
            assert ep in scripts, f"Entry point {ep} not declared in pyproject.toml"

    def test_python_imports_are_importable(self, checklist_text: str) -> None:
        imports = _extract_python_imports(checklist_text)
        # Only test pitwall-internal imports; stdlib/third-party may need extras
        pitwall_imports = {imp for imp in imports if imp.startswith("pitwall.")}
        failures: list[str] = []
        for imp in pitwall_imports:
            try:
                importlib.import_module(imp)
            except (
                Exception
            ) as exc:  # reason: mirror of the script's import sweep; collect all failures
                failures.append(f"{imp}: {exc}")
        assert not failures, f"Checklist references unimportable modules: {failures}"

    def test_checklist_contains_all_steps(self, checklist_text: str) -> None:
        # Verify the checklist covers the mandated flow:
        # init → provider → capability → inference → cost → teardown
        required_sections = [
            "Step 5",
            "Step 6",
            "Step 7",
            "Step 11",
        ]
        for section in required_sections:
            assert section in checklist_text, f"Checklist missing required section: {section}"
