"""Static guardrails for immutable operational audit tables."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ARCHIVE_SOURCE = _REPO_ROOT / "src" / "pitwall" / "retention" / "archive.py"


def test_archive_module_is_valid_python() -> None:
    ast.parse(_ARCHIVE_SOURCE.read_text(encoding="utf-8"))


def test_purge_never_deletes_kill_log() -> None:
    source = _ARCHIVE_SOURCE.read_text(encoding="utf-8").lower()
    assert "delete from pitwall.kill_log" not in source


def test_purge_never_deletes_config_audit() -> None:
    source = _ARCHIVE_SOURCE.read_text(encoding="utf-8").lower()
    assert "delete from pitwall.config_audit" not in source


def test_purge_never_drops_schema_objects() -> None:
    source = _ARCHIVE_SOURCE.read_text(encoding="utf-8").lower()
    assert "drop table" not in source
    assert "drop schema" not in source
