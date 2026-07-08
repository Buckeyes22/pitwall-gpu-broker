"""Tests for the reconciler module entrypoint."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

import pitwall.reconciler.__main__ as reconciler_main
from pitwall.reconciler import WorkerSettings


def test_main_runs_arq_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = MagicMock()
    create_worker = MagicMock(return_value=worker)
    monkeypatch.setattr(reconciler_main, "_ARQ_AVAILABLE", True)
    monkeypatch.setattr(reconciler_main, "create_worker", create_worker)
    monkeypatch.setattr(sys, "argv", ["pitwall-reconciler"])

    reconciler_main.main()

    create_worker.assert_called_once_with(WorkerSettings)
    worker.run.assert_called_once_with()


def test_main_check_mode_exits_with_config_check_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_redis_config = MagicMock(return_value=7)
    monkeypatch.setattr(reconciler_main, "check_redis_config", check_redis_config)
    monkeypatch.setattr(sys, "argv", ["pitwall-reconciler", "check"])

    with pytest.raises(SystemExit) as exc_info:
        reconciler_main.main()

    assert exc_info.value.code == 7
    check_redis_config.assert_called_once_with()


def test_main_fails_when_arq_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(reconciler_main, "_ARQ_AVAILABLE", False)
    monkeypatch.setattr(reconciler_main, "create_worker", None)
    monkeypatch.setattr(sys, "argv", ["pitwall-reconciler"])

    with pytest.raises(SystemExit) as exc_info:
        reconciler_main.main()

    assert exc_info.value.code == 1
    assert "arq is not installed; cannot run worker" in capsys.readouterr().err
