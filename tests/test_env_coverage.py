from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"


def _parse_env_example(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", stripped)
        if m:
            names.add(m.group(1))
    return names


def _extract_env_refs(src_dir: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for py_file in src_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            name = _env_name_from_node(node)
            if name is not None:
                rel = str(py_file.relative_to(src_dir))
                result.setdefault(rel, set()).add(name)
    return result


def _env_name_from_node(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and (
            isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
            and node.value.attr == "environ"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        )
    ):
        return node.slice.value

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        func = node.func
        if (
            isinstance(func.value, ast.Name)
            and func.value.id == "os"
            and func.attr == "getenv"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return node.args[0].value
        if (
            isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os"
            and func.value.attr == "environ"
            and func.attr == "get"
        ) and (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return node.args[0].value

    return None


def test_env_example_is_parseable():
    assert _ENV_EXAMPLE.is_file(), ".env.example must exist at repo root"
    names = _parse_env_example(_ENV_EXAMPLE)
    assert len(names) > 0, ".env.example must contain at least one variable"


def test_every_code_env_ref_documented_in_env_example():
    documented = _parse_env_example(_ENV_EXAMPLE)
    code_refs = _extract_env_refs(_SRC_DIR)

    undocumented: dict[str, set[str]] = {}
    for file_path, env_names in sorted(code_refs.items()):
        missing = env_names - documented
        if missing:
            undocumented[file_path] = missing

    assert not undocumented, (
        "env vars referenced in code but missing from .env.example:\n"
        + "\n".join(f"  {f}: {', '.join(sorted(v))}" for f, v in sorted(undocumented.items()))
    )


def test_core_runtime_env_vars_present_in_env_example():
    from pitwall.config import _CORE_RUNTIME_ENV

    documented = _parse_env_example(_ENV_EXAMPLE)
    missing = set(_CORE_RUNTIME_ENV) - documented
    assert not missing, (
        f"core runtime env vars missing from .env.example: {', '.join(sorted(missing))}"
    )


def test_no_duplicate_entries_in_env_example():
    seen: dict[str, int] = {}
    for line in _ENV_EXAMPLE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", stripped)
        if m:
            name = m.group(1)
            seen[name] = seen.get(name, 0) + 1

    dupes = {k: v for k, v in seen.items() if v > 1}
    assert not dupes, f"duplicate entries in .env.example: {dupes}"
