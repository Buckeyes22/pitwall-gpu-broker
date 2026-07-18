"""Policy document loading helpers."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pitwall.policy.schema import Policy, PolicySet
from pitwall.seed import SeedValidationError, _parse_simple_yaml


class PolicyLoadError(ValueError):
    """Raised when a policy document cannot be parsed or validated."""


def load_policy_file(path: str | Path) -> PolicySet:
    """Load one JSON/YAML policy document from *path*."""

    policy_path = Path(path)
    text = policy_path.read_text(encoding="utf-8")
    return _load_policy_text(text, source=str(policy_path))


def load_policy_files(paths: Iterable[str | Path]) -> PolicySet:
    """Load and merge policy documents from *paths* in caller-provided order."""

    return merge_policy_sets(load_policy_file(path) for path in paths)


def load_default_policy_set() -> PolicySet:
    """Load the packaged example policies used by the audit gate."""

    examples = files("pitwall.policy").joinpath("examples")
    documents = [
        _load_policy_text(resource.read_text(encoding="utf-8"), source=resource.name)
        for resource in sorted(examples.iterdir(), key=lambda item: item.name)
        if resource.name.endswith((".json", ".yaml", ".yml"))
    ]
    return merge_policy_sets(documents)


def merge_policy_sets(policy_sets: Iterable[PolicySet]) -> PolicySet:
    """Merge policy documents into one deterministic policy set."""

    policies: list[Policy] = []
    version = 1
    for policy_set in policy_sets:
        version = max(version, policy_set.version)
        policies.extend(policy_set.policies)
    return PolicySet(version=version, policies=tuple(policies))


def _load_policy_text(text: str, *, source: str) -> PolicySet:
    try:
        payload = _parse_policy_payload(text)
        if not isinstance(payload, Mapping):
            raise PolicyLoadError(f"{source}: top-level policy document must be an object")
        return PolicySet.model_validate(payload)
    except (json.JSONDecodeError, SeedValidationError, ValidationError, ValueError) as exc:
        if isinstance(exc, PolicyLoadError):
            raise
        raise PolicyLoadError(f"{source}: invalid policy document: {exc}") from exc


def _parse_policy_payload(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped[0] in "[{":
        return json.loads(stripped)
    return _parse_simple_yaml(stripped)


__all__ = [
    "PolicyLoadError",
    "load_default_policy_set",
    "load_policy_file",
    "load_policy_files",
    "merge_policy_sets",
]
