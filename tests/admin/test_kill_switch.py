from __future__ import annotations

import pytest

from pitwall.api.admin.kill_switch import CloudKillSwitch, KillReport


class _FakeTS:
    def __init__(
        self,
        acl_raises: bool = False,
        devices: int = 2,
        revoke_raises: bool = False,
        compute_n: int = 0,
        compute_raises: bool = False,
    ) -> None:
        self.acl_raises = acl_raises
        self.revoke_raises = revoke_raises
        self.devices = devices
        self.compute_n = compute_n
        self.compute_raises = compute_raises

    async def deny_all(self, tag: str) -> bool:
        if self.acl_raises:
            raise RuntimeError("acl fail")
        return True

    async def revoke_devices(self, tag: str) -> int:
        if self.revoke_raises:
            raise RuntimeError("revoke fail")
        return self.devices


class _Clock:
    def __init__(self, *values: float) -> None:
        self._values = list(values)

    def perf_counter(self) -> float:
        return self._values.pop(0)


@pytest.mark.anyio
async def test_kill_switch_constructs_without_tailnet_config() -> None:
    ks = CloudKillSwitch(terminate_compute=False)

    rep = await ks.activate("no tailnet configured")

    assert rep.tailscale_acl_updated is False
    assert rep.devices_removed == 0
    assert rep.pods_terminated == 0
    assert rep.errors == []


@pytest.mark.anyio
async def test_kill_switch_atomic_three_step_under_30s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_terminate_all(name_prefix: str) -> int:
        return 1

    # Patch the exact module-globals dict activate() resolves names in, so the
    # mock is immune to sys.modules being swapped by an earlier test that
    # deletes+reimports pitwall.api.* (string-path setattr would miss it).
    monkeypatch.setitem(
        CloudKillSwitch.activate.__globals__,
        "terminate_all_with_tag",
        fake_terminate_all,
    )
    ks = CloudKillSwitch(_FakeTS(devices=2, compute_n=1), terminate_compute=True)
    rep = await ks.activate("test drill")
    assert isinstance(rep, KillReport)
    assert rep.tailscale_acl_updated is True
    assert rep.devices_removed == 2
    assert rep.pods_terminated == 1
    assert rep.total_duration_ms < 30000
    assert rep.errors == []


@pytest.mark.anyio
async def test_kill_switch_requires_reason() -> None:
    ks = CloudKillSwitch(_FakeTS(), terminate_compute=False)
    with pytest.raises(ValueError):
        await ks.activate("")


@pytest.mark.anyio
async def test_kill_switch_partial_failure_continues() -> None:
    ks = CloudKillSwitch(_FakeTS(acl_raises=True), terminate_compute=False)
    rep = await ks.activate("partial")
    assert rep.tailscale_acl_updated is False
    assert rep.devices_removed == 2
    assert any(err.startswith("acl:") for err in rep.errors)


@pytest.mark.anyio
async def test_kill_switch_collects_all_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_terminate_fail(name_prefix: str) -> int:
        raise RuntimeError("compute fail")

    monkeypatch.setitem(
        CloudKillSwitch.activate.__globals__,
        "terminate_all_with_tag",
        fake_terminate_fail,
    )
    ks = CloudKillSwitch(
        _FakeTS(acl_raises=True, revoke_raises=True),
        terminate_compute=True,
    )
    rep = await ks.activate("all fail")
    assert rep.tailscale_acl_updated is False
    assert len(rep.errors) >= 2
    assert any(err.startswith("acl:") for err in rep.errors)
    assert any(err.startswith("devices:") for err in rep.errors)


@pytest.mark.anyio
async def test_kill_switch_duration_uses_deterministic_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pitwall.api.admin import kill_switch

    clock = _Clock(100.0, 100.125)

    async def fake_terminate_all(name_prefix: str) -> int:
        return 1

    async def fake_get_pods(name_prefix: str) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(kill_switch.time, "perf_counter", clock.perf_counter)
    monkeypatch.setitem(
        CloudKillSwitch.activate.__globals__, "terminate_all_with_tag", fake_terminate_all
    )
    monkeypatch.setitem(
        CloudKillSwitch.activate.__globals__, "get_pods_by_tag_prefix", fake_get_pods
    )

    ks = CloudKillSwitch(_FakeTS(devices=2), terminate_compute=True)
    rep = await ks.activate("deterministic duration")

    assert rep.total_duration_ms == 125
    assert rep.total_duration_ms < 30000
    assert rep.errors == []


@pytest.mark.anyio
async def test_kill_switch_stage_order_acl_devices_compute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    events: list[str] = []

    class _OrderedTS:
        async def deny_all(self, tag: str) -> bool:
            events.append("acl-deny")
            return True

        async def revoke_devices(self, tag: str) -> int:
            events.append("device-removal")
            return 2

    async def fake_terminate_all(name_prefix: str) -> int:
        events.append("compute-termination")
        return 1

    async def fake_get_pods(name_prefix: str) -> list[dict[str, object]]:
        return []

    monkeypatch.setitem(
        CloudKillSwitch.activate.__globals__, "terminate_all_with_tag", fake_terminate_all
    )
    monkeypatch.setitem(
        CloudKillSwitch.activate.__globals__, "get_pods_by_tag_prefix", fake_get_pods
    )

    ks = CloudKillSwitch(_OrderedTS(), terminate_compute=True)
    rep = await ks.activate("stage order")

    assert events == ["acl-deny", "device-removal", "compute-termination"]
    assert rep.tailscale_acl_updated is True
    assert rep.devices_removed == 2
    assert rep.pods_terminated == 1
