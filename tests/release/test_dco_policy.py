"""DCO policy parser regression tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.release


def _module() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "tools" / "ci" / "check_dco.py"
    spec = importlib.util.spec_from_file_location("check_dco", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_dco_parser_accepts_valid_trailer_and_rejects_missing() -> None:
    module = _module()
    commits = module.parse_git_log(
        "a" * 40
        + "\0fix: safe change\n\nSigned-off-by: Example Contributor <dev@example.com>\n\0"
        + "b" * 40
        + "\0fix: unsigned change\n\0"
    )
    assert module.unsigned_commits(commits) == ["b" * 40]


def test_dco_parser_rejects_malformed_identity() -> None:
    module = _module()
    commits = module.parse_git_log(
        "c" * 40 + "\0docs: update\n\nSigned-off-by: missing-address\n\0"
    )
    assert module.unsigned_commits(commits) == ["c" * 40]
