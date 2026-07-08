"""Fail when a candidate OpenAPI document removes a public contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    baseline_paths = baseline.get("paths", {})
    candidate_paths = candidate.get("paths", {})
    for path, old_item in baseline_paths.items():
        if path not in candidate_paths:
            errors.append(f"removed path: {path}")
            continue
        new_item = candidate_paths[path]
        for method, old_operation in old_item.items():
            if method not in HTTP_METHODS:
                continue
            if method not in new_item:
                errors.append(f"removed operation: {method.upper()} {path}")
                continue
            new_operation = new_item[method]
            old_successes = {
                status
                for status in old_operation.get("responses", {})
                if str(status).startswith("2")
            }
            new_successes = set(new_operation.get("responses", {}))
            for status in sorted(old_successes - new_successes):
                errors.append(f"removed success response: {method.upper()} {path} {status}")

            old_required = _required_request_fields(old_operation)
            new_required = _required_request_fields(new_operation)
            for field in sorted(new_required - old_required):
                errors.append(f"new required field: {method.upper()} {path} {field}")
    return errors


def _required_request_fields(operation: dict[str, Any]) -> set[str]:
    content = operation.get("requestBody", {}).get("content", {})
    schema = content.get("application/json", {}).get("schema", {})
    return set(schema.get("required", []))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    errors = compare(baseline, candidate)
    for error in errors:
        print(f"OpenAPI compatibility failure: {error}", file=sys.stderr)
    if not errors:
        print("OpenAPI compatibility passed")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
