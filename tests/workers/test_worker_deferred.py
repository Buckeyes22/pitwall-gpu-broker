"""The deferred GPU worker must never look healthy to old automation."""

from __future__ import annotations

import pytest

from pitwall import worker


def test_legacy_worker_entrypoint_fails_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert worker.main(["--type", "llm"]) == worker.EX_UNAVAILABLE
    captured = capsys.readouterr()
    assert "unavailable in the public alpha" in captured.err
    assert "0002-worker-deferred.md" in captured.err
