"""Inventory the installed runtime graph and enforce the release license policy."""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "tools" / "security" / "license-policy.json"

CLASSIFIER_LICENSES = {
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD",
    "ISC License (ISCL)": "ISC",
    "MIT License": "MIT",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
}
RAW_LICENSES = {
    "Apache 2.0": "Apache-2.0",
    "Apache License 2.0": "Apache-2.0",
    "MIT License": "MIT",
}


def _license(dist: Distribution) -> str:
    expression = dist.metadata.get("License-Expression")
    if expression:
        return expression.strip()
    raw = (dist.metadata.get("License") or "").strip()
    if raw and raw != "Dual License" and len(raw) <= 120 and "\n" not in raw:
        return RAW_LICENSES.get(raw, raw)
    classifier_values: list[str] = []
    for classifier in dist.metadata.get_all("Classifier", []):
        prefix = "License :: OSI Approved :: "
        if classifier.startswith(prefix):
            classifier_values.append(
                CLASSIFIER_LICENSES.get(classifier.removeprefix(prefix), classifier)
            )
    if classifier_values:
        return " OR ".join(classifier_values)
    return "UNKNOWN"


def runtime_graph(root_name: str) -> list[Distribution]:
    """Resolve installed, marker-active runtime dependencies from one root."""

    queue = deque([canonicalize_name(root_name)])
    visited: set[str] = set()
    result: list[Distribution] = []
    while queue:
        name = queue.popleft()
        if name in visited:
            continue
        visited.add(name)
        try:
            dist = distribution(name)
        except PackageNotFoundError as exc:
            raise RuntimeError(f"runtime dependency is not installed: {name}") from exc
        result.append(dist)
        for raw_requirement in dist.requires or []:
            requirement = Requirement(raw_requirement)
            if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                continue
            queue.append(canonicalize_name(requirement.name))
    return sorted(result, key=lambda item: canonicalize_name(item.metadata["Name"]))


def evaluate(rows: list[dict[str, str]], policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    allowed = tuple(policy["allowed_license_terms"])
    denied = tuple(policy["denied_license_terms"])
    review = {
        canonicalize_name(name): {
            "version": str(expected["version"]),
            "license": str(expected["license"]),
        }
        for name, expected in policy["review_required_packages"].items()
    }
    seen_review: set[str] = set()
    for row in rows:
        name = canonicalize_name(row["name"])
        license_value = row["license"]
        if any(term in license_value for term in denied):
            errors.append(f"{name} {row['version']}: denied license {license_value!r}")
            continue
        if name in review:
            seen_review.add(name)
            expected = review[name]
            if row["version"] != expected["version"]:
                errors.append(
                    f"{name}: version changed from reviewed {expected['version']!r} "
                    f"to {row['version']!r}"
                )
            if license_value != expected["license"]:
                errors.append(
                    f"{name} {row['version']}: license changed from reviewed "
                    f"{expected['license']!r} to {license_value!r}"
                )
            continue
        if license_value == "UNKNOWN" or not any(term in license_value for term in allowed):
            errors.append(
                f"{name} {row['version']}: unknown or unapproved license {license_value!r}"
            )
    missing = sorted(set(review) - seen_review)
    errors.extend(f"review-required package missing from runtime graph: {name}" for name in missing)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="pitwall-gpu-broker")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    rows = [
        {
            "name": dist.metadata["Name"],
            "version": dist.version,
            "license": _license(dist),
        }
        for dist in runtime_graph(args.root)
    ]
    errors = evaluate(rows, policy)
    report = {
        "root": args.root,
        "status": "pass" if not errors else "fail",
        "legal_approval": "project-owner-approved exact graph; see docs/legal/transitive-license-review.md",
        "packages": rows,
        "errors": errors,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    for error in errors:
        print(f"license policy failed: {error}", file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
