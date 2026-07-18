"""Chaos: terminate is idempotent when the pod is already gone."""

from __future__ import annotations

import pytest

import pitwall.runpod_client.pods as pods
from pitwall.runpod_client.pods import RunPodError, RunPodRestError, terminate_pod_sync

pytestmark = [pytest.mark.anyio, pytest.mark.chaos]


def test_terminate_404_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_404(method: str, path: str, **kwargs: object) -> None:
        raise RunPodRestError(method, path, 404, "not found")

    monkeypatch.setattr(pods, "_rest_request", _raise_404)

    assert terminate_pod_sync("pod-gone") is None


def test_terminate_500_raises_runpoderror(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_500(method: str, path: str, **kwargs: object) -> None:
        raise RunPodRestError(method, path, 500, "server error")

    monkeypatch.setattr(pods, "_rest_request", _raise_500)

    with pytest.raises(RunPodError):
        terminate_pod_sync("pod-x")


def test_terminate_success_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def _ok(method: str, path: str, **kwargs: object) -> dict[str, object]:
        calls.append((method, path))
        return {}

    monkeypatch.setattr(pods, "_rest_request", _ok)

    assert terminate_pod_sync("pod-1") is None
    assert calls == [("DELETE", "pods/pod-1")]
