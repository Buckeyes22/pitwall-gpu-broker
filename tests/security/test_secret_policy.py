from __future__ import annotations

import subprocess

from tools.security import check_secrets


def test_candidate_files_include_tracked_and_nonignored_untracked(monkeypatch) -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=b"tracked.py\0new.py\0",
        stderr=b"",
    )
    monkeypatch.setattr(check_secrets.subprocess, "run", lambda *_args, **_kwargs: completed)

    assert check_secrets._candidate_files() == ["tracked.py", "new.py"]
