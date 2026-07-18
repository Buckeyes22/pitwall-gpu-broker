"""Enforce weighted line and branch floors for high-risk source domains."""

from __future__ import annotations

import argparse
import fnmatch
import json
from pathlib import Path
from typing import Any


def _percent(covered: int, total: int) -> float:
    return 100.0 if total == 0 else covered * 100.0 / total


def evaluate(coverage: dict[str, Any], policy: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    files: dict[str, Any] = coverage.get("files", {})
    report: dict[str, Any] = {}
    failures: list[str] = []
    for domain, config in sorted(policy.items()):
        patterns = config["patterns"]
        matched = sorted(
            name for name in files if any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
        )
        if not matched:
            failures.append(f"{domain}: no coverage files matched {patterns}")
            continue
        statements = sum(int(files[name]["summary"]["num_statements"]) for name in matched)
        missing_lines = sum(int(files[name]["summary"]["missing_lines"]) for name in matched)
        branches = sum(int(files[name]["summary"]["num_branches"]) for name in matched)
        missing_branches = sum(int(files[name]["summary"]["missing_branches"]) for name in matched)
        line_percent = _percent(statements - missing_lines, statements)
        branch_percent = _percent(branches - missing_branches, branches)
        line_floor = float(config["minimum_line_percent"])
        branch_floor = float(config["minimum_branch_percent"])
        report[domain] = {
            "files": matched,
            "line_percent": round(line_percent, 2),
            "line_floor": line_floor,
            "branch_percent": round(branch_percent, 2),
            "branch_floor": branch_floor,
        }
        if line_percent < line_floor:
            failures.append(
                f"{domain}: line coverage {line_percent:.2f}% is below {line_floor:.2f}%"
            )
        if branch_percent < branch_floor:
            failures.append(
                f"{domain}: branch coverage {branch_percent:.2f}% is below {branch_floor:.2f}%"
            )
    return report, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("tools/ci/risk-coverage-policy.json"),
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    coverage = json.loads(args.coverage_json.read_text(encoding="utf-8"))
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    report, failures = evaluate(coverage, policy)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    for failure in failures:
        print(f"risk-coverage-error: {failure}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
