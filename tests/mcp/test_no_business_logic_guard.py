"""No-business-logic guard for MCP handlers.

Fail tests when MCP handlers import RunPod clients, routing internals,
or cost estimators directly. The MCP layer must be a thin wrapper that
delegates to service-layer functions; it must not contain business logic
or import implementation details directly.

Forbidden imports:
  - pitwall.runpod_client.*          (RunPod client internals)
  - pitwall.routing.*                (routing internals)
  - pitwall.cost.budget_gate         (cost estimator)
  - pitwall.cost.sync_gate           (cost estimator)
  - pitwall.cost.estimator           (cost estimator)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MCP_TOOLS_DIR = _REPO_ROOT / "src" / "pitwall" / "mcp" / "tools"

_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "pitwall.runpod_client",
    "pitwall.routing",
    "pitwall.cost.budget_gate",
    "pitwall.cost.sync_gate",
    "pitwall.cost.estimator",
)


def _get_imports_from_file(filepath: Path) -> list[tuple[str, int]]:
    """Return list of (full_import_name, line_number) for all imports in a file."""
    try:
        source = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    imports: list[tuple[str, int]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                full_name = f"{module}.{alias.name}" if module else alias.name
                imports.append((full_name, node.lineno))

    return imports


def _is_forbidden(import_name: str) -> bool:
    """Return True if the import is forbidden."""
    for prefix in _FORBIDDEN_PREFIXES:
        if import_name == prefix or import_name.startswith(f"{prefix}."):
            return True
    return False


class TestNoBusinessLogicImports:
    """Verify MCP tools do not import forbidden business-logic modules."""

    @pytest.fixture()
    def tool_files(self) -> list[Path]:
        """Return all Python files in the MCP tools directory."""
        if not _MCP_TOOLS_DIR.is_dir():
            return []
        return sorted(_MCP_TOOLS_DIR.glob("*.py"))

    @pytest.fixture()
    def tool_imports(self, tool_files: list[Path]) -> dict[Path, list[tuple[str, int]]]:
        """Return a mapping of tool file to its imports."""
        return {f: _get_imports_from_file(f) for f in tool_files}

    def test_tools_directory_exists(self, tool_files: list[Path]) -> None:
        assert _MCP_TOOLS_DIR.is_dir(), f"MCP tools directory not found: {_MCP_TOOLS_DIR}"

    def test_no_runpod_client_imports(
        self, tool_imports: dict[Path, list[tuple[str, int]]]
    ) -> None:
        """MCP tools must not import pitwall.runpod_client or its submodules."""
        violations: list[str] = []
        for filepath, imports in tool_imports.items():
            for import_name, lineno in imports:
                if import_name == "pitwall.runpod_client" or import_name.startswith(
                    "pitwall.runpod_client."
                ):
                    violations.append(f"  {filepath.name}:{lineno}: imports '{import_name}'")

        assert not violations, (
            "MCP handlers must not import RunPod client internals:\n" + "\n".join(violations)
        )

    def test_no_routing_imports(self, tool_imports: dict[Path, list[tuple[str, int]]]) -> None:
        """MCP tools must not import pitwall.routing or its submodules."""
        violations: list[str] = []
        for filepath, imports in tool_imports.items():
            for import_name, lineno in imports:
                if import_name == "pitwall.routing" or import_name.startswith("pitwall.routing."):
                    violations.append(f"  {filepath.name}:{lineno}: imports '{import_name}'")

        assert not violations, "MCP handlers must not import routing internals:\n" + "\n".join(
            violations
        )

    def test_no_cost_estimator_imports(
        self, tool_imports: dict[Path, list[tuple[str, int]]]
    ) -> None:
        """MCP tools must not import cost estimator internals directly."""
        cost_prefixes = (
            "pitwall.cost.budget_gate",
            "pitwall.cost.sync_gate",
            "pitwall.cost.estimator",
        )
        violations: list[str] = []
        for filepath, imports in tool_imports.items():
            for import_name, lineno in imports:
                for prefix in cost_prefixes:
                    if import_name == prefix or import_name.startswith(f"{prefix}."):
                        violations.append(f"  {filepath.name}:{lineno}: imports '{import_name}'")
                        break

        assert not violations, (
            "MCP handlers must not import cost estimator internals directly:\n"
            + "\n".join(violations)
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
