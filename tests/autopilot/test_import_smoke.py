"""Fresh-process import smoke tests for the Autopilot package."""

from __future__ import annotations

import subprocess
import sys


def test_autopilot_package_imports_in_fresh_process() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib; importlib.import_module('pitwall.autopilot')",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
