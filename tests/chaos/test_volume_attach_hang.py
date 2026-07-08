"""Chaos: zero-uptime network-volume pods raise PodVolumeAttachTimeout."""

from __future__ import annotations

import pytest

import pitwall.runpod_client.pods as pods
from pitwall.runpod_client.pods import PodVolumeAttachTimeout, wait_for_pod_runtime_sync

pytestmark = [pytest.mark.anyio, pytest.mark.chaos]


def test_attach_hang_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    hung_pod = {
        "id": "pod-vol-1",
        "networkVolumeId": "vol-1",
        "desiredStatus": "RUNNING",
        "runtime": {"uptimeInSeconds": 0},
    }
    monkeypatch.setattr(pods, "get_pod_sync", lambda _pod_id: dict(hung_pod))

    ticks = iter(range(0, 10_000, 10))
    monkeypatch.setattr(pods.time, "monotonic", lambda: float(next(ticks)))
    monkeypatch.setattr(pods.time, "sleep", lambda _seconds: None)

    with pytest.raises(PodVolumeAttachTimeout) as exc_info:
        wait_for_pod_runtime_sync(
            "pod-vol-1",
            initial=dict(hung_pod),
            timeout_s=600.0,
            poll_s=1.0,
            volume_attach_timeout_s=0.0,
        )

    assert exc_info.value.pod_id == "pod-vol-1"
