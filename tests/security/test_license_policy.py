"""License policy rejects unknown, denied, and drifted reviewed licenses."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _module() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "tools" / "security" / "check_licenses.py"
    spec = importlib.util.spec_from_file_location("check_licenses", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _policy() -> dict[str, object]:
    return {
        "allowed_license_terms": ["MIT", "Apache-2.0"],
        "denied_license_terms": ["AGPL"],
        "review_required_packages": {"special": {"version": "2", "license": "MPL-2.0"}},
    }


def test_policy_accepts_allowlist_and_exact_review() -> None:
    rows = [
        {"name": "normal", "version": "1", "license": "MIT"},
        {"name": "special", "version": "2", "license": "MPL-2.0"},
    ]
    assert _module().evaluate(rows, _policy()) == []


def test_policy_rejects_unknown_denied_and_review_drift() -> None:
    rows = [
        {"name": "unknown", "version": "1", "license": "UNKNOWN"},
        {"name": "denied", "version": "1", "license": "AGPL-3.0"},
        {"name": "special", "version": "2", "license": "MPL-2.0+"},
    ]
    errors = _module().evaluate(rows, _policy())
    assert len(errors) == 3


def test_policy_rejects_reviewed_package_version_drift() -> None:
    rows = [{"name": "special", "version": "3", "license": "MPL-2.0"}]
    errors = _module().evaluate(rows, _policy())
    assert errors == ["special: version changed from reviewed '2' to '3'"]
