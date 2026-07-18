"""Hermetic check that private evidence/spec documents are not shipped."""

from __future__ import annotations

from pathlib import Path

_PRIVATE_DOC_GLOBS = ("PRIVATE-*.md", "*_integration-spec-*.md")


def test_private_evidence_docs_are_cut_from_public_tree() -> None:
    for pattern in _PRIVATE_DOC_GLOBS:
        matches = [*Path(".").glob(pattern), *Path("docs").rglob(pattern)]
        assert not matches, f"private document(s) present in public tree: {matches}"
