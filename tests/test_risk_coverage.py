from __future__ import annotations

from tools.ci.check_risk_coverage import evaluate


def _coverage(*, missing_lines: int = 1, missing_branches: int = 1) -> dict[str, object]:
    return {
        "files": {
            "src/pitwall/high_risk.py": {
                "summary": {
                    "num_statements": 10,
                    "missing_lines": missing_lines,
                    "num_branches": 4,
                    "missing_branches": missing_branches,
                }
            }
        }
    }


def test_evaluate_passes_weighted_floors() -> None:
    policy = {
        "risk": {
            "patterns": ["src/pitwall/high_*.py"],
            "minimum_line_percent": 90,
            "minimum_branch_percent": 75,
        }
    }
    report, failures = evaluate(_coverage(), policy)
    assert failures == []
    assert report["risk"]["line_percent"] == 90.0
    assert report["risk"]["branch_percent"] == 75.0


def test_evaluate_fails_floor_and_missing_pattern() -> None:
    policy = {
        "risk": {
            "patterns": ["src/pitwall/high_*.py"],
            "minimum_line_percent": 95,
            "minimum_branch_percent": 80,
        },
        "missing": {
            "patterns": ["src/pitwall/absent.py"],
            "minimum_line_percent": 1,
            "minimum_branch_percent": 1,
        },
    }
    _, failures = evaluate(_coverage(), policy)
    assert len(failures) == 3
    assert any("no coverage files matched" in failure for failure in failures)
