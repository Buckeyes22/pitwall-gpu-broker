"""Reject pull-request commits that lack a Developer Certificate sign-off."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass

SIGN_OFF = re.compile(
    r"^Signed-off-by:\s+\S(?:.*\S)?\s+<[^<>@\s]+@[^<>\s]+>$",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class CommitMessage:
    sha: str
    body: str


def parse_git_log(raw: str) -> list[CommitMessage]:
    """Parse alternating NUL-separated commit SHA and full message fields."""

    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise ValueError("git log output did not contain SHA/message pairs")
    return [CommitMessage(fields[index], fields[index + 1]) for index in range(0, len(fields), 2)]


def unsigned_commits(commits: list[CommitMessage]) -> list[str]:
    return [commit.sha for commit in commits if SIGN_OFF.search(commit.body) is None]


def _commits(base: str, head: str) -> list[CommitMessage]:
    result = subprocess.run(
        [
            "git",
            "log",
            "--no-merges",
            "-z",
            "--format=%H%x00%B",
            f"{base}..{head}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_git_log(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("PITWALL_DCO_BASE_SHA"))
    parser.add_argument("--head", default=os.environ.get("PITWALL_DCO_HEAD_SHA"))
    args = parser.parse_args(argv)
    if not args.base or not args.head:
        parser.error("--base/--head or PITWALL_DCO_BASE_SHA/PITWALL_DCO_HEAD_SHA are required")
    try:
        commits = _commits(args.base, args.head)
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(f"unable to inspect pull-request commits: {exc}", file=sys.stderr)
        return 2
    unsigned = unsigned_commits(commits)
    for sha in unsigned:
        print(f"{sha}: missing a valid 'Signed-off-by: Name <email>' trailer", file=sys.stderr)
    if not unsigned:
        print(f"DCO sign-off passed for {len(commits)} non-merge commit(s)")
    return int(bool(unsigned))


if __name__ == "__main__":
    raise SystemExit(main())
