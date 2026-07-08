#!/usr/bin/env python
"""Mutation-score gate for the release program core trio.

Reads ``mutants/mutmut-cicd-stats.json`` (produced by ``mutmut export-cicd-stats``
after a ``mutmut run``) and fails if the kill rate over *covered* mutants falls
below a floor.

Score = killed / (killed + survived). ``no_tests`` mutants (lines our curated
hermetic oracle does not exercise) are reported but excluded from the ratio: the
floor is a statement about the logic the oracle covers, not about coverage
breadth. ``timeout``/``suspicious`` are folded into the denominator as
not-killed so a mutant that hangs cannot inflate the score.

Usage:
    python scripts/mutmut_score_gate.py --floor 85
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_STATS_PATH = Path("mutants/mutmut-cicd-stats.json")


def _load_stats(path: Path) -> dict[str, int]:
    if not path.exists():
        sys.stderr.write(
            f"error: {path} not found — run `mutmut run && mutmut export-cicd-stats` first\n"
        )
        raise SystemExit(2)
    return json.loads(path.read_text())


def compute_score(stats: dict[str, int]) -> tuple[float, int, int, int]:
    killed = int(stats.get("killed", 0))
    survived = int(stats.get("survived", 0))
    timeout = int(stats.get("timeout", 0))
    suspicious = int(stats.get("suspicious", 0))
    no_tests = int(stats.get("no_tests", 0))
    not_killed = survived + timeout + suspicious
    covered = killed + not_killed
    score = 100.0 * killed / covered if covered else 0.0
    return score, killed, covered, no_tests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail if mutation score is below the floor.")
    parser.add_argument("--floor", type=float, default=85.0, help="Minimum kill %% (default 85)")
    parser.add_argument("--stats", type=Path, default=_STATS_PATH, help="cicd-stats.json path")
    args = parser.parse_args(argv)

    stats = _load_stats(args.stats)
    score, killed, covered, no_tests = compute_score(stats)

    print(
        f"mutation score: {score:.1f}% ({killed}/{covered} covered mutants killed; "
        f"{no_tests} uncovered/no-test mutants excluded)"
    )
    if score < args.floor:
        sys.stderr.write(f"FAIL: mutation score {score:.1f}% < floor {args.floor:.1f}%\n")
        return 1
    print(f"PASS: mutation score {score:.1f}% >= floor {args.floor:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
