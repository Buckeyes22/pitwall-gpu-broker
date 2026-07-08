from __future__ import annotations

import logging

import httpx
import pytest

from pitwall.runpod_client import pods
from pitwall.runpod_client.workloads import WorkloadConfig
from tests.fakes.runpod import FakePodStateMachine, RunPodRestFake

pytestmark = pytest.mark.anyio


def _workload() -> WorkloadConfig:
    return WorkloadConfig(
        name="test",
        capability="test",
        gpu_types=["NVIDIA L4"],
        container_disk_gb=10,
        min_vcpu=1,
        min_memory_gb=1,
        cloud_type="SECURE",
        allowed_cuda_versions=["12.8", "12.9"],
    )


def test_runpod_resource_error_is_retryable_capacity_error() -> None:
    exc = pods.RunPodRestError(
        "POST",
        "pods",
        500,
        '{"error":"create pod: This machine does not have the resources to deploy your pod. Please try a different machine"}',
    )

    assert pods._is_capacity_error(exc)


def test_capacity_error_matcher_list_can_be_overridden() -> None:
    exc = pods.RunPodRestError(
        "POST",
        "pods",
        503,
        '{"error":"capacity pool empty for requested GPU family"}',
    )

    def match_new_wording(candidate: Exception) -> bool:
        return (
            isinstance(candidate, pods.RunPodRestError) and "capacity pool empty" in candidate.body
        )

    assert pods._is_capacity_error(exc, matchers=[match_new_wording])


def test_capacity_error_substrings_can_be_configured_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        pods.CAPACITY_ERROR_SUBSTRINGS_ENV,
        "capacity pool empty for requested GPU family",
    )
    exc = pods.RunPodRestError(
        "POST",
        "pods",
        503,
        '{"error":"capacity pool empty for requested GPU family"}',
    )

    assert pods._is_capacity_error(exc)


def test_unmatched_capacity_error_logs_full_rest_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = '{"error":"new-runpod-error-shape","requestId":"req-full-body"}'
    exc = pods.RunPodRestError("POST", "pods", 502, body)
    caplog.set_level(logging.WARNING, logger="pitwall.runpod_client.pods")

    assert not pods._is_capacity_error(exc, matchers=[])

    assert "unmatched RunPod error while checking capacity match" in caplog.text
    assert "body=" + body in caplog.text


def test_get_pod_merges_sdk_runtime_when_rest_omits_it(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    class FakeSdk:
        @staticmethod
        def get_pod(pod_id: str) -> dict:
            assert pod_id == "pod-1"
            return {
                "id": "pod-1",
                "runtime": {"ports": [{"privatePort": 8000, "type": "http"}]},
                "uptimeSeconds": 0,
            }

    runpod_rest_fake.add(
        "GET",
        "pods/pod-1",
        {"id": "pod-1", "desiredStatus": "RUNNING", "runtime": None},
    )
    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods, "_sdk", lambda: FakeSdk())

    pod = pods.get_pod_sync("pod-1")

    assert pod is not None
    assert pod["runtime"] == {"ports": [{"privatePort": 8000, "type": "http"}]}
    assert pod["uptimeSeconds"] == 0


def test_get_pod_uses_explicit_credentials_when_env_key_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    calls: list[dict[str, object]] = []

    def fake_rest_request(
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
        timeout_s: float = 60.0,
        api_key: str | None = None,
        rest_api_url: str | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "method": method,
                "path": path,
                "json_body": json_body,
                "params": params,
                "timeout_s": timeout_s,
                "api_key": api_key,
                "rest_api_url": rest_api_url,
            }
        )
        return {
            "id": "pod-1",
            "desiredStatus": "RUNNING",
            "runtime": {"podStatus": "RUNNING"},
            "portMappings": {"8000": 12345},
        }

    monkeypatch.setattr(pods, "_rest_request", fake_rest_request)

    pod = pods._get_pod_sync(
        "pod-1",
        api_key="plugin-key",
        rest_api_url="https://rest.runpod.test/v1",
        strict_errors=True,
    )

    assert pod is not None
    assert pod["id"] == "pod-1"
    assert calls == [
        {
            "method": "GET",
            "path": "pods/pod-1",
            "json_body": None,
            "params": {"includeMachine": "true", "includeNetworkVolume": "true"},
            "timeout_s": 60.0,
            "api_key": "plugin-key",
            "rest_api_url": "https://rest.runpod.test/v1",
        }
    ]


def test_get_pod_strict_errors_only_treats_404_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_rest_request(
        method: str,
        path: str,
        **_: object,
    ) -> dict[str, object]:
        raise pods.RunPodRestError(method, path, 401, '{"error":"unauthorized"}')

    monkeypatch.setattr(pods, "_rest_request", fake_rest_request)

    with pytest.raises(pods.RunPodRestError, match="HTTP 401"):
        pods._get_pod_sync("pod-auth-failure", api_key="plugin-key", strict_errors=True)


def test_terminate_pod_for_auth_uses_explicit_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_rest_request(
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
        timeout_s: float = 60.0,
        api_key: str | None = None,
        rest_api_url: str | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "method": method,
                "path": path,
                "json_body": json_body,
                "params": params,
                "timeout_s": timeout_s,
                "api_key": api_key,
                "rest_api_url": rest_api_url,
            }
        )
        return {}

    monkeypatch.setattr(pods, "_rest_request", fake_rest_request)

    pods._terminate_pod_sync_for_auth(
        "pod-1",
        api_key="plugin-key",
        rest_api_url="https://rest.runpod.test/v1",
    )

    assert calls == [
        {
            "method": "DELETE",
            "path": "pods/pod-1",
            "json_body": None,
            "params": None,
            "timeout_s": 60.0,
            "api_key": "plugin-key",
            "rest_api_url": "https://rest.runpod.test/v1",
        }
    ]


def test_runtime_with_null_ports_is_not_ready() -> None:
    assert not pods._pod_has_runtime({"desiredStatus": "RUNNING", "runtime": {"ports": None}})


def test_zero_uptime_is_detected_from_runtime_or_top_level() -> None:
    assert pods._pod_has_zero_uptime({"runtime": {"uptimeInSeconds": 0}})
    assert pods._pod_has_zero_uptime({"uptimeSeconds": "0"})
    assert not pods._pod_has_zero_uptime({"runtime": {"uptimeInSeconds": 12}})


def test_http_proxy_200_counts_as_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Response:
        status_code = 200

    def fake_get(url: str, **kwargs):  # type: ignore[no-untyped-def]  # reason: fake accepts arbitrary HTTP kwargs
        calls.append(url)
        return Response()

    monkeypatch.setattr(pods.httpx, "get", fake_get)

    pod = {"id": "pod-1", "ports": ["8000/http"]}

    assert pods._pod_http_proxy_ready(pod)
    assert calls == ["https://pod-1-8000.proxy.runpod.net/health"]
    assert pod["proxyUrl"] == "https://pod-1-8000.proxy.runpod.net"


def test_http_proxy_404_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status_code = 404

    monkeypatch.setattr(pods.httpx, "get", lambda *args, **kwargs: Response())

    assert not pods._pod_http_proxy_ready({"id": "pod-1", "ports": ["8000/http"]})


def test_pod_readiness_probe_uses_ssh_first_then_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_ssh_probe(pod: dict[str, object]) -> pods.PodProbeResult:
        calls.append(pods.SSH_LOCALHOST_PROBE_METHOD)
        return pods.PodProbeResult(
            healthy=False,
            method=pods.SSH_LOCALHOST_PROBE_METHOD,
            error="not_configured",
        )

    def fake_proxy_probe(pod: dict[str, object]) -> pods.PodProbeResult:
        calls.append(pods.PROXY_PROBE_METHOD)
        return pods.PodProbeResult(healthy=True, method=pods.PROXY_PROBE_METHOD)

    monkeypatch.setattr(pods, "_pod_ssh_localhost_probe", fake_ssh_probe)
    monkeypatch.setattr(pods, "_pod_http_proxy_probe", fake_proxy_probe)

    result = pods._pod_readiness_probe({"id": "pod-1", "ports": ["8000/http"]})

    assert result.healthy
    assert result.method == pods.PROXY_PROBE_METHOD
    assert calls == [pods.SSH_LOCALHOST_PROBE_METHOD, pods.PROXY_PROBE_METHOD]


def test_wait_for_pod_runtime_tracks_readiness_timestamps_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter(
        [
            "2026-05-26T14:00:18Z",
            "2026-05-26T14:00:19Z",
            "2026-05-26T14:00:34Z",
        ]
    )
    snapshots = [
        {
            "id": "pod-1",
            "desiredStatus": "RUNNING",
            "ports": ["8000/http"],
            "runtime": {"podStatus": "RUNNING"},
        },
        {
            "id": "pod-1",
            "desiredStatus": "RUNNING",
            "ports": ["8000/http"],
            "runtime": {"podStatus": "RUNNING"},
            "portMappings": {"8000": 12345},
        },
    ]

    class Response:
        status_code = 200

    monkeypatch.setattr(pods, "_utc_now_iso", lambda: next(timestamps))
    monkeypatch.setattr(pods.time, "sleep", lambda _: None)
    monkeypatch.setattr(pods.httpx, "get", lambda *args, **kwargs: Response())
    monkeypatch.setattr(pods, "get_pod_sync", lambda pod_id: snapshots.pop(0))

    pod = pods.wait_for_pod_runtime_sync(
        "pod-1",
        initial={"id": "pod-1", "desiredStatus": "RUNNING", "ports": ["8000/http"]},
        timeout_s=1,
        poll_s=0.001,
    )

    assert pod["readiness"] == {
        "runtime_seen_at": "2026-05-26T14:00:18Z",
        "port_mappings_seen_at": "2026-05-26T14:00:19Z",
        "probe_passed_at": "2026-05-26T14:00:34Z",
        "probe_method": "runpod_proxy",
    }


async def test_create_pod_waits_until_runtime_exists(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "image": "image:sha",
            "gpu": {"id": "NVIDIA L4", "displayName": "NVIDIA L4"},
            "machine": {"dataCenterId": "TEST"},
        },
    )
    runpod_rest_fake.add(
        "GET",
        "pods/pod-1",
        {
            "id": "pod-1",
            "desiredStatus": "RUNNING",
            "lastStartedAt": "2026-05-18T12:00:00Z",
            "runtime": {"podStatus": "RUNNING"},
            "portMappings": {"8000": 12345},
        },
    )

    class Response:
        status_code = 200

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods.httpx, "get", lambda *args, **kwargs: Response())

    pod = await pods.create_pod_with_fallback(
        name="pitwall-test",
        template_id="template-1",
        image_name="image:sha",
        workload=_workload(),
        env={"ENV_VAR": "value"},
        network_volume_id="volume-1",
        data_center_id="US-CA-2",
        docker_entrypoint=["/bin/sh", "-lc"],
        docker_start_cmd=["echo ok"],
        container_registry_auth_id="registry-auth-1",
        support_public_ip=True,
        startup_timeout_s=0.2,
        startup_poll_s=0.01,
    )

    assert pod["portMappings"] == {"8000": 12345}
    assert set(pod["readiness"]) == {
        "runtime_seen_at",
        "port_mappings_seen_at",
        "probe_passed_at",
        "probe_method",
    }
    post_payload = runpod_rest_fake.calls[0].json_body
    assert post_payload is not None
    assert post_payload["templateId"] == "template-1"
    assert post_payload["imageName"] == "image:sha"
    assert post_payload["gpuTypeIds"] == ["NVIDIA L4"]
    assert post_payload["gpuTypePriority"] == "custom"
    assert post_payload["allowedCudaVersions"] == ["12.8", "12.9"]
    assert post_payload["containerRegistryAuthId"] == "registry-auth-1"
    assert post_payload["dataCenterIds"] == ["US-CA-2"]
    assert post_payload["dataCenterPriority"] == "custom"
    assert post_payload["networkVolumeId"] == "volume-1"
    assert post_payload["volumeMountPath"] == "/workspace"
    assert post_payload["supportPublicIp"] is True
    assert post_payload["dockerEntrypoint"] == ["/bin/sh", "-lc"]
    assert post_payload["dockerStartCmd"] == ["echo ok"]


async def test_create_pod_can_use_runpod_availability_priority(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "image": "image:sha",
            "gpu": {"id": "NVIDIA RTX A4000", "displayName": "NVIDIA RTX A4000"},
            "machine": {"dataCenterId": "TEST"},
            "runtime": {
                "podStatus": "RUNNING",
                "ports": [{"privatePort": 8000, "type": "http"}],
            },
            "portMappings": {"8000": 12345},
        },
    )

    class Response:
        status_code = 200

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods.httpx, "get", lambda *args, **kwargs: Response())

    workload = WorkloadConfig(
        name="test",
        capability="test",
        gpu_types=["NVIDIA L4", "NVIDIA RTX A4000"],
        gpu_type_priority="availability",
        data_center_priority="availability",
        container_disk_gb=10,
        min_vcpu=1,
        min_memory_gb=1,
        cloud_type="SECURE",
    )

    pod = await pods.create_pod_with_fallback(
        name="pitwall-test",
        template_id=None,
        image_name="image:sha",
        workload=workload,
        env={},
        data_center_id="US-CA-2",
        startup_timeout_s=0.2,
        startup_poll_s=0.01,
    )

    assert pod["id"] == "pod-1"
    post_payload = runpod_rest_fake.calls[0].json_body
    assert post_payload is not None
    assert post_payload["gpuTypeIds"] == ["NVIDIA L4", "NVIDIA RTX A4000"]
    assert post_payload["gpuTypePriority"] == "availability"
    assert post_payload["dataCenterPriority"] == "availability"


async def test_create_pod_terminates_stuck_runtime(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods",
        {"id": "pod-1", "name": "pitwall-test", "desiredStatus": "RUNNING"},
    )
    runpod_rest_fake.add(
        "GET",
        "pods/pod-1",
        {"id": "pod-1", "desiredStatus": "RUNNING", "lastStartedAt": None},
    )
    runpod_rest_fake.add("DELETE", "pods/pod-1", {})

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    with pytest.raises(pods.NoCapacityError):
        await pods.create_pod_with_fallback(
            name="pitwall-test",
            template_id="template-1",
            image_name="image:sha",
            workload=_workload(),
            env={},
            startup_timeout_s=0.01,
            startup_poll_s=0.001,
        )

    assert runpod_rest_fake.deleted_pod_ids == ["pod-1"]


async def test_create_pod_terminates_when_pre_readiness_callback_raises(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "desiredStatus": "RUNNING",
            "gpu": {"id": "NVIDIA L4", "displayName": "NVIDIA L4"},
            "machine": {"dataCenterId": "TEST"},
        },
    )
    runpod_rest_fake.add("DELETE", "pods/pod-1", {})

    def failing_pre_readiness_callback(pod: dict[str, object]) -> None:
        assert pod["id"] == "pod-1"
        raise RuntimeError("lease persist failed")

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    with pytest.raises(RuntimeError, match="lease persist failed"):
        await pods.create_pod_with_fallback(
            name="pitwall-test",
            template_id="template-1",
            image_name="image:sha",
            workload=_workload(),
            env={},
            startup_timeout_s=1,
            startup_poll_s=0.001,
            pre_readiness_callback=failing_pre_readiness_callback,
        )

    assert [(call.method, call.path) for call in runpod_rest_fake.calls] == [
        ("POST", "pods"),
        ("DELETE", "pods/pod-1"),
    ]
    assert runpod_rest_fake.deleted_pod_ids == ["pod-1"]


async def test_create_pod_terminates_volume_attach_hang_before_startup_timeout(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods",
        {"id": "pod-1", "name": "pitwall-test", "desiredStatus": "RUNNING"},
    )
    runpod_rest_fake.add(
        "GET",
        "pods/pod-1",
        {
            "id": "pod-1",
            "desiredStatus": "RUNNING",
            "networkVolumeId": "volume-1",
            "runtime": {"uptimeInSeconds": 0},
        },
    )
    runpod_rest_fake.add("DELETE", "pods/pod-1", {})

    monkeypatch.setattr(pods.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(pods.time, "sleep", lambda _: None)
    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    with pytest.raises(pods.ProviderAttachHangRecoveryRequested) as exc_info:
        await pods.create_pod_with_fallback(
            name="pitwall-test",
            template_id="template-1",
            image_name="image:sha",
            workload=_workload(),
            env={},
            network_volume_id="volume-1",
            startup_timeout_s=30,
            startup_poll_s=0.001,
            volume_attach_timeout_s=0.0,
        )

    assert exc_info.value.pod_id == "pod-1"
    assert exc_info.value.attach_timeout_s == 0.0
    get_calls = [
        call
        for call in runpod_rest_fake.calls
        if call.method == "GET" and call.path == "pods/pod-1"
    ]
    assert len(get_calls) == 1
    assert runpod_rest_fake.deleted_pod_ids == ["pod-1"]


async def test_create_pod_enforces_cost_cap_before_readiness_wait(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    readiness_waits: list[str] = []

    def fail_wait_for_pod_runtime_sync(
        pod_id: str,
        *,
        initial: dict[str, object] | None = None,
        timeout_s: float = 600.0,
        poll_s: float = 15.0,
    ) -> dict[str, object]:
        readiness_waits.append(pod_id)
        raise AssertionError("cost-capped pod must not wait for readiness")

    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "desiredStatus": "RUNNING",
            "costPerHr": 9.99,
        },
    )
    runpod_rest_fake.add("DELETE", "pods/pod-1", {})

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods, "wait_for_pod_runtime_sync", fail_wait_for_pod_runtime_sync)

    with pytest.raises(pods.RunPodError, match="pod attempt limit"):
        await pods.create_pod_with_fallback(
            name="pitwall-test",
            template_id="template-1",
            image_name="image:sha",
            workload=_workload(),
            env={},
            max_cost_per_hr=1.0,
            max_pod_attempts=1,
            startup_timeout_s=1,
            startup_poll_s=0.001,
        )

    calls = [(call.method, call.path) for call in runpod_rest_fake.calls]
    assert runpod_rest_fake.deleted_pod_ids == ["pod-1"]
    assert ("GET", "pods/pod-1") not in calls
    assert readiness_waits == []


async def test_create_pod_explicit_auth_cost_cap_cleanup_uses_explicit_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rest_calls: list[dict[str, object]] = []
    responses = [
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "desiredStatus": "RUNNING",
            "costPerHr": 9.99,
        },
        {},
    ]

    def fake_rest_request(
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
        timeout_s: float = 60.0,
        api_key: str | None = None,
        rest_api_url: str | None = None,
    ) -> dict[str, object]:
        rest_calls.append(
            {
                "method": method,
                "path": path,
                "json_body": json_body,
                "params": params,
                "timeout_s": timeout_s,
                "api_key": api_key,
                "rest_api_url": rest_api_url,
            }
        )
        return responses.pop(0)

    def fail_wait_for_pod_runtime_sync(
        pod_id: str,
        *,
        initial: dict[str, object] | None = None,
        timeout_s: float = 600.0,
        poll_s: float = 15.0,
    ) -> dict[str, object]:
        raise AssertionError("cost-capped pod must not wait for readiness")

    monkeypatch.setattr(pods, "_rest_request", fake_rest_request)
    monkeypatch.setattr(pods, "wait_for_pod_runtime_sync", fail_wait_for_pod_runtime_sync)

    with pytest.raises(pods.RunPodError, match="pod attempt limit"):
        await pods._create_pod_with_fallback(
            name="pitwall-test",
            template_id="template-1",
            image_name="image:sha",
            workload=_workload(),
            env={},
            max_cost_per_hr=1.0,
            max_pod_attempts=1,
            startup_timeout_s=1,
            startup_poll_s=0.001,
            api_key="plugin-key",
            rest_api_url="https://rest.runpod.test/v1",
        )

    assert [
        (call["method"], call["path"], call["api_key"], call["rest_api_url"]) for call in rest_calls
    ] == [
        ("POST", "pods", "plugin-key", "https://rest.runpod.test/v1"),
        ("DELETE", "pods/pod-1", "plugin-key", "https://rest.runpod.test/v1"),
    ]


async def test_create_pod_enforces_cost_cap_before_probe_loop(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runtime_polls: list[str] = []
    probe_attempts: list[dict[str, object]] = []

    def fail_get_pod_sync(pod_id: str) -> dict[str, object]:
        runtime_polls.append(pod_id)
        raise AssertionError("cost-capped pod must not poll runtime")

    def fail_http_proxy_probe(pod: dict[str, object]) -> bool:
        probe_attempts.append(pod)
        raise AssertionError("cost-capped pod must not enter probe loop")

    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "desiredStatus": "RUNNING",
            "cost_per_hr": "3.50",
        },
    )
    runpod_rest_fake.add("DELETE", "pods/pod-1", {})

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods, "get_pod_sync", fail_get_pod_sync)
    monkeypatch.setattr(pods, "_pod_http_proxy_ready", fail_http_proxy_probe)

    with pytest.raises(pods.RunPodError, match="pod attempt limit"):
        await pods.create_pod_with_fallback(
            name="pitwall-test",
            template_id="template-1",
            image_name="image:sha",
            workload=_workload(),
            env={},
            max_cost_per_hr=1.0,
            max_pod_attempts=1,
            startup_timeout_s=1,
            startup_poll_s=0.001,
        )

    calls = [(call.method, call.path) for call in runpod_rest_fake.calls]
    assert runpod_rest_fake.deleted_pod_ids == ["pod-1"]
    assert runtime_polls == []
    assert probe_attempts == []
    assert calls == [("POST", "pods"), ("DELETE", "pods/pod-1")]


async def test_create_pod_enforces_cost_cap_from_pre_readiness_refresh(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    readiness_waits: list[str] = []

    def fail_wait_for_pod_runtime_sync(
        pod_id: str,
        *,
        initial: dict[str, object] | None = None,
        timeout_s: float = 600.0,
        poll_s: float = 15.0,
    ) -> dict[str, object]:
        readiness_waits.append(pod_id)
        raise AssertionError("cost-capped pod must not wait for readiness")

    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "desiredStatus": "RUNNING",
            "gpu": {"id": "NVIDIA L4", "displayName": "NVIDIA L4"},
        },
    )
    runpod_rest_fake.add(
        "GET",
        "pods/pod-1",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "desiredStatus": "RUNNING",
            "gpuTypeId": "NVIDIA L4",
            "costPerHr": 3.50,
        },
    )
    runpod_rest_fake.add("DELETE", "pods/pod-1", {})

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods, "wait_for_pod_runtime_sync", fail_wait_for_pod_runtime_sync)

    with pytest.raises(pods.ProviderFallbackRequested, match="provider fallback requested"):
        await pods.create_pod_with_fallback(
            name="pitwall-test",
            template_id="template-1",
            image_name="image:sha",
            workload=_workload(),
            env={},
            max_cost_per_hr=1.0,
            startup_timeout_s=1,
            startup_poll_s=0.001,
        )

    calls = [(call.method, call.path) for call in runpod_rest_fake.calls]
    assert runpod_rest_fake.deleted_pod_ids == ["pod-1"]
    assert readiness_waits == []
    assert calls == [("POST", "pods"), ("GET", "pods/pod-1"), ("DELETE", "pods/pod-1")]


async def test_create_pod_terminates_zero_gpu_allocation_before_readiness_wait(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    readiness_waits: list[str] = []

    def fail_wait_for_pod_runtime_sync(
        pod_id: str,
        *,
        initial: dict[str, object] | None = None,
        timeout_s: float = 600.0,
        poll_s: float = 15.0,
    ) -> dict[str, object]:
        readiness_waits.append(pod_id)
        raise AssertionError("zero-GPU pod must not wait for readiness")

    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "desiredStatus": "RUNNING",
            "machine": {"dataCenterId": "TEST"},
        },
    )
    runpod_rest_fake.add(
        "GET",
        "pods/pod-1",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "desiredStatus": "RUNNING",
            "gpuCount": 0,
        },
    )
    runpod_rest_fake.add("DELETE", "pods/pod-1", {})

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods, "wait_for_pod_runtime_sync", fail_wait_for_pod_runtime_sync)

    with pytest.raises(pods.ProviderFallbackRequested, match="zero GPUs"):
        await pods.create_pod_with_fallback(
            name="pitwall-test",
            template_id="template-1",
            image_name="image:sha",
            workload=_workload(),
            env={},
            startup_timeout_s=1,
            startup_poll_s=0.001,
        )

    calls = [(call.method, call.path) for call in runpod_rest_fake.calls]
    assert runpod_rest_fake.deleted_pod_ids == ["pod-1"]
    assert readiness_waits == []
    assert calls == [("POST", "pods"), ("GET", "pods/pod-1"), ("DELETE", "pods/pod-1")]


async def test_create_pod_skips_readiness_for_over_cap_attempt_before_next_gpu(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    readiness_waits: list[str] = []
    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-expensive",
            "name": "pitwall-test",
            "desiredStatus": "RUNNING",
            "costPerHr": 9.99,
        },
        {
            "id": "pod-ok",
            "name": "pitwall-test",
            "desiredStatus": "RUNNING",
            "costPerHr": 0.50,
        },
    )
    runpod_rest_fake.add("DELETE", "pods/pod-expensive", {})

    def fake_wait_for_pod_runtime_sync(
        pod_id: str,
        *,
        initial: dict[str, object] | None = None,
        timeout_s: float = 600.0,
        poll_s: float = 15.0,
    ) -> dict[str, object]:
        readiness_waits.append(pod_id)
        return {
            **(initial or {}),
            "id": pod_id,
            "readiness": {
                "runtime_seen_at": "2026-05-28T12:00:00Z",
                "port_mappings_seen_at": "2026-05-28T12:00:01Z",
                "probe_passed_at": "2026-05-28T12:00:02Z",
            },
        }

    workload = WorkloadConfig(
        name="test",
        capability="test",
        gpu_types=["NVIDIA L4", "NVIDIA RTX A4000"],
        container_disk_gb=10,
        min_vcpu=1,
        min_memory_gb=1,
        cloud_type="SECURE",
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods, "wait_for_pod_runtime_sync", fake_wait_for_pod_runtime_sync)

    pod = await pods.create_pod_with_fallback(
        name="pitwall-test",
        template_id="template-1",
        image_name="image:sha",
        workload=workload,
        env={},
        max_cost_per_hr=1.0,
        startup_timeout_s=1,
        startup_poll_s=0.001,
    )

    calls = [(call.method, call.path) for call in runpod_rest_fake.calls]
    assert pod["id"] == "pod-ok"
    assert runpod_rest_fake.deleted_pod_ids == ["pod-expensive"]
    assert readiness_waits == ["pod-ok"]
    assert calls == [
        ("POST", "pods"),
        ("DELETE", "pods/pod-expensive"),
        ("POST", "pods"),
    ]


async def test_create_pod_fails_fast_when_pod_disappears_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods",
        {"id": "pod-1", "name": "pitwall-test", "desiredStatus": "RUNNING"},
    )
    runpod_rest_fake.add(
        "GET",
        "pods/pod-1",
        pods.RunPodRestError("GET", "pods/pod-1", 404, "not found"),
    )
    runpod_rest_fake.add("DELETE", "pods/pod-1", {})

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    with pytest.raises(pods.PodStartupFailed):
        await pods.create_pod_with_fallback(
            name="pitwall-test",
            template_id="template-1",
            image_name="image:sha",
            workload=_workload(),
            env={},
            startup_timeout_s=1,
            startup_poll_s=0.001,
        )

    assert runpod_rest_fake.deleted_pod_ids == ["pod-1"]


async def test_create_pod_terminates_on_transient_poll_error(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods",
        {"id": "pod-1", "name": "pitwall-test", "desiredStatus": "RUNNING"},
    )
    runpod_rest_fake.add(
        "GET",
        "pods/pod-1",
        pods.RunPodRestError("GET", "pods/pod-1", 500, "temporary outage"),
    )
    runpod_rest_fake.add("DELETE", "pods/pod-1", {})
    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    with pytest.raises(pods.RunPodRestError) as exc_info:
        await pods.create_pod_with_fallback(
            name="pitwall-test",
            template_id="template-1",
            image_name="image:sha",
            workload=_workload(),
            env={},
            startup_timeout_s=1,
            startup_poll_s=0.001,
        )

    assert exc_info.value.status_code == 500
    assert runpod_rest_fake.deleted_pod_ids == ["pod-1"]


async def test_cost_cap_compares_high_precision_decimal_prices(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    readiness_waits: list[str] = []

    def fail_wait_for_pod_runtime_sync(
        pod_id: str,
        *,
        initial: dict[str, object] | None = None,
        timeout_s: float = 600.0,
        poll_s: float = 15.0,
    ) -> dict[str, object]:
        readiness_waits.append(pod_id)
        raise AssertionError("over-cap pod must not wait for readiness")

    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "desiredStatus": "RUNNING",
            "costPerHr": "0.100000000000000005",
        },
    )
    runpod_rest_fake.add("DELETE", "pods/pod-1", {})
    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods, "wait_for_pod_runtime_sync", fail_wait_for_pod_runtime_sync)

    with pytest.raises(pods.RunPodError, match="pod attempt limit"):
        await pods.create_pod_with_fallback(
            name="pitwall-test",
            template_id="template-1",
            image_name="image:sha",
            workload=_workload(),
            env={},
            max_cost_per_hr=0.1,
            max_pod_attempts=1,
            startup_timeout_s=1,
            startup_poll_s=0.001,
        )

    assert runpod_rest_fake.deleted_pod_ids == ["pod-1"]
    assert readiness_waits == []


# --- Cold-start simulation tests -----------------------------------
#
# These use FakePodStateMachine to simulate a pod starting from a cold
# (PENDING) state and transitioning to RUNNING, matching the  AC
# selector ``-k cold_start``.


async def test_cold_start_simulates_pending_to_running_transition(
    fake_pod_state_machine: FakePodStateMachine,
) -> None:
    fake_pod_state_machine.states = ["PENDING", "RUNNING"]
    fake_pod_state_machine.index = 0

    async with httpx.AsyncClient(
        transport=fake_pod_state_machine.transport(),
        base_url="https://rest.runpod.io/v1",
    ) as client:
        cold = await client.get("/pods/pod-test")
        assert cold.json()["desiredStatus"] == "PENDING"
        assert cold.json()["runtime"] is None

        advanced = await client.post("/pods")
        assert advanced.json()["desiredStatus"] == "RUNNING"

        warm = await client.get("/pods/pod-test")
        assert warm.json()["runtime"]["ports"][0]["ip"] == "127.0.0.1"


async def test_cold_start_with_multi_state_progression(
    fake_pod_state_machine: FakePodStateMachine,
) -> None:
    fake_pod_state_machine.states = ["PENDING", "STARTING", "RUNNING"]
    fake_pod_state_machine.index = 0

    async with httpx.AsyncClient(
        transport=fake_pod_state_machine.transport(),
        base_url="https://rest.runpod.io/v1",
    ) as client:
        first = await client.get("/pods/pod-test")
        assert first.json()["desiredStatus"] == "PENDING"

        second = await client.post("/pods")
        assert second.json()["desiredStatus"] == "STARTING"

        third = await client.post("/pods")
        assert third.json()["desiredStatus"] == "RUNNING"
        assert third.json()["runtime"]["ports"][0]["privatePort"] == 8000


async def test_cold_start_clamps_at_final_state(
    fake_pod_state_machine: FakePodStateMachine,
) -> None:
    fake_pod_state_machine.states = ["PENDING", "RUNNING"]
    fake_pod_state_machine.index = 0

    async with httpx.AsyncClient(
        transport=fake_pod_state_machine.transport(),
        base_url="https://rest.runpod.io/v1",
    ) as client:
        await client.post("/pods")
        await client.post("/pods")
        await client.post("/pods")

        steady = await client.get("/pods/pod-test")
        assert steady.json()["desiredStatus"] == "RUNNING"


# --- Landmine invariant tests ------------------------------------
#
# These tests verify the hard invariants encoded in pods.py that prevent
# RunPod operational landmines from being triggered at runtime.


async def test_l2_cloud_type_forced_to_secure_when_volume_attached(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    """L2: cloud_type=ALL + networkVolumeId wastes 50%% of fallback attempts.

    RunPod requires SECURE when a volume is attached. The wrapper forces
    cloud_type=SECURE whenever network_volume_id is set, preventing the
    community+secure fallback loop that burns money.
    """
    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "image": "image:sha",
            "gpu": {"id": "NVIDIA L4", "displayName": "NVIDIA L4"},
            "machine": {"dataCenterId": "US-KS-2"},
            "runtime": {
                "podStatus": "RUNNING",
                "ports": [{"privatePort": 8000, "type": "http"}],
            },
            "portMappings": {"8000": 12345},
        },
    )

    class Response:
        status_code = 200

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods.httpx, "get", lambda *args, **kwargs: Response())

    workload_with_all_cloud = WorkloadConfig(
        name="test",
        capability="test",
        gpu_types=["NVIDIA L4"],
        container_disk_gb=10,
        min_vcpu=1,
        min_memory_gb=1,
        cloud_type="ALL",
    )

    pod = await pods.create_pod_with_fallback(
        name="pitwall-test",
        template_id=None,
        image_name="image:sha",
        workload=workload_with_all_cloud,
        env={},
        network_volume_id="vol_abc123",
        data_center_id="US-KS-2",
        startup_timeout_s=0.2,
        startup_poll_s=0.01,
    )

    assert pod["id"] == "pod-1"
    post_payload = runpod_rest_fake.calls[0].json_body
    assert post_payload is not None
    assert post_payload["computeType"] == "GPU"
    assert post_payload["cloudType"] == "SECURE"
    assert post_payload["networkVolumeId"] == "vol_abc123"
    assert post_payload["dataCenterIds"] == ["US-KS-2"]
    assert post_payload["dataCenterPriority"] == "custom"


async def test_l2_raises_when_community_cloud_type_with_volume(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    """L2: COMMUNITY cloud_type with a volume is rejected by RunPod policy.

    The wrapper raises RunPodError before sending the request, preventing
    a guaranteed failure.
    """
    runpod_rest_fake.add(
        "POST",
        "pods",
        {"id": "pod-1"},
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    workload_community = WorkloadConfig(
        name="test",
        capability="test",
        gpu_types=["NVIDIA L4"],
        container_disk_gb=10,
        min_vcpu=1,
        min_memory_gb=1,
        cloud_type="COMMUNITY",
    )

    with pytest.raises(pods.RunPodError, match="SECURE"):
        await pods.create_pod_with_fallback(
            name="pitwall-test",
            template_id=None,
            image_name="image:sha",
            workload=workload_community,
            env={},
            network_volume_id="vol_abc123",
            startup_timeout_s=0.2,
            startup_poll_s=0.01,
        )


async def test_l10_volume_mount_path_is_workspace_for_pods(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    """L10: Pods mount network volumes at /workspace; Serverless uses /runpod-volume.

    The pod create wrapper hard-codes volumeMountPath=/workspace so consumers
    never need to know which provider type uses which path.
    """
    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "image": "image:sha",
            "gpu": {"id": "NVIDIA L4", "displayName": "NVIDIA L4"},
            "machine": {"dataCenterId": "US-KS-2"},
            "runtime": {
                "podStatus": "RUNNING",
                "ports": [{"privatePort": 8000, "type": "http"}],
            },
            "portMappings": {"8000": 12345},
        },
    )

    class Response:
        status_code = 200

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods.httpx, "get", lambda *args, **kwargs: Response())

    pod = await pods.create_pod_with_fallback(
        name="pitwall-test",
        template_id=None,
        image_name="image:sha",
        workload=_workload(),
        env={},
        network_volume_id="vol_abc123",
        data_center_id="US-KS-2",
        startup_timeout_s=0.2,
        startup_poll_s=0.01,
    )

    assert pod["id"] == "pod-1"
    post_payload = runpod_rest_fake.calls[0].json_body
    assert post_payload is not None
    assert post_payload["volumeMountPath"] == "/workspace"


async def test_single_dc_volume_pin_sends_single_dc_id_and_custom_priority(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    """Single-DC volume pinning: dataCenterIds=[single_dc] + dataCenterPriority=custom.

    Multi-DC volume sync does not work in RunPod. When a volume is attached,
    the wrapper pins to a single DC with custom priority so the volume is
    actually reachable.
    """
    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "image": "image:sha",
            "gpu": {"id": "NVIDIA H100 80GB HBM3", "displayName": "NVIDIA H100 80GB HBM3"},
            "machine": {"dataCenterId": "US-KS-2"},
            "runtime": {
                "podStatus": "RUNNING",
                "ports": [{"privatePort": 8000, "type": "http"}],
            },
            "portMappings": {"8000": 12345},
        },
    )

    class Response:
        status_code = 200

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods.httpx, "get", lambda *args, **kwargs: Response())

    workload = WorkloadConfig(
        name="test",
        capability="test",
        gpu_types=["NVIDIA H100 80GB HBM3"],
        container_disk_gb=10,
        min_vcpu=1,
        min_memory_gb=1,
        cloud_type="SECURE",
        data_center_priority="custom",
    )

    pod = await pods.create_pod_with_fallback(
        name="pitwall-test",
        template_id=None,
        image_name="image:sha",
        workload=workload,
        env={},
        network_volume_id="vol_abc123",
        data_center_id="US-KS-2",
        startup_timeout_s=0.2,
        startup_poll_s=0.01,
    )

    assert pod["id"] == "pod-1"
    post_payload = runpod_rest_fake.calls[0].json_body
    assert post_payload is not None
    assert post_payload["dataCenterIds"] == ["US-KS-2"]
    assert post_payload["dataCenterPriority"] == "custom"
    assert post_payload["networkVolumeId"] == "vol_abc123"


async def test_l1_canonical_gpu_names_are_sent_to_runpod(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    """L1: GPU IDs must be canonical full names; abbreviations silently fail.

    WorkloadConfig validates canonical GPU names at construction time.
    This test verifies the validated names are passed correctly to the
    RunPod REST API.
    """
    runpod_rest_fake.add(
        "POST",
        "pods",
        {
            "id": "pod-1",
            "name": "pitwall-test",
            "image": "image:sha",
            "gpu": {"id": "NVIDIA H100 80GB HBM3", "displayName": "NVIDIA H100 80GB HBM3"},
            "machine": {"dataCenterId": "US-KS-2"},
            "runtime": {
                "podStatus": "RUNNING",
                "ports": [{"privatePort": 8000, "type": "http"}],
            },
            "portMappings": {"8000": 12345},
        },
    )

    class Response:
        status_code = 200

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)
    monkeypatch.setattr(pods.httpx, "get", lambda *args, **kwargs: Response())

    workload = WorkloadConfig(
        name="test",
        capability="test",
        gpu_types=["NVIDIA H100 80GB HBM3"],
        container_disk_gb=10,
        min_vcpu=1,
        min_memory_gb=1,
        cloud_type="SECURE",
    )

    pod = await pods.create_pod_with_fallback(
        name="pitwall-test",
        template_id=None,
        image_name="image:sha",
        workload=workload,
        env={},
        data_center_id="US-KS-2",
        startup_timeout_s=0.2,
        startup_poll_s=0.01,
    )

    assert pod["id"] == "pod-1"
    post_payload = runpod_rest_fake.calls[0].json_body
    assert post_payload is not None
    assert post_payload["gpuTypeIds"] == ["NVIDIA H100 80GB HBM3"]


# --- Pod lifecycle start/stop/reset/restart/update tests ---------------------


async def test_start_pod_sync_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods/pod-1/start",
        {"id": "pod-1", "desiredStatus": "RUNNING"},
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    result = await pods.start_pod("pod-1")

    assert result["id"] == "pod-1"
    assert result["desiredStatus"] == "RUNNING"
    call = runpod_rest_fake.calls[0]
    assert call.method == "POST"
    assert call.path == "pods/pod-1/start"


async def test_start_pod_wraps_rest_error(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods/pod-1/start",
        pods.RunPodRestError("POST", "pods/pod-1/start", 500, "internal error"),
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    with pytest.raises(pods.RunPodError, match="start_pod"):
        await pods.start_pod("pod-1")


async def test_stop_pod_sync_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods/pod-1/stop",
        {"id": "pod-1", "desiredStatus": "STOPPED"},
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    result = await pods.stop_pod("pod-1")

    assert result["id"] == "pod-1"
    assert result["desiredStatus"] == "STOPPED"
    call = runpod_rest_fake.calls[0]
    assert call.method == "POST"
    assert call.path == "pods/pod-1/stop"


async def test_stop_pod_wraps_rest_error(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods/pod-1/stop",
        pods.RunPodRestError("POST", "pods/pod-1/stop", 503, "service unavailable"),
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    with pytest.raises(pods.RunPodError, match="stop_pod"):
        await pods.stop_pod("pod-1")


async def test_reset_pod_sync_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods/pod-1/reset",
        {"id": "pod-1", "desiredStatus": "RUNNING"},
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    result = await pods.reset_pod("pod-1")

    assert result["id"] == "pod-1"
    call = runpod_rest_fake.calls[0]
    assert call.method == "POST"
    assert call.path == "pods/pod-1/reset"


async def test_reset_pod_wraps_rest_error(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods/pod-1/reset",
        pods.RunPodRestError("POST", "pods/pod-1/reset", 500, "reset failed"),
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    with pytest.raises(pods.RunPodError, match="reset_pod"):
        await pods.reset_pod("pod-1")


async def test_restart_pod_sync_calls_stop_then_start(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "POST",
        "pods/pod-1/stop",
        {"id": "pod-1", "desiredStatus": "STOPPED"},
    )
    runpod_rest_fake.add(
        "POST",
        "pods/pod-1/start",
        {"id": "pod-1", "desiredStatus": "RUNNING"},
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    result = await pods.restart_pod("pod-1")

    assert result["id"] == "pod-1"
    assert result["desiredStatus"] == "RUNNING"
    assert len(runpod_rest_fake.calls) == 2
    assert runpod_rest_fake.calls[0].path == "pods/pod-1/stop"
    assert runpod_rest_fake.calls[0].method == "POST"
    assert runpod_rest_fake.calls[1].path == "pods/pod-1/start"
    assert runpod_rest_fake.calls[1].method == "POST"


async def test_update_pod_sync_env_only(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "PATCH",
        "pods/pod-1",
        {"id": "pod-1", "desiredStatus": "RUNNING"},
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    result = await pods.update_pod("pod-1", env={"FOO": "bar"})

    assert result["id"] == "pod-1"
    call = runpod_rest_fake.calls[0]
    assert call.method == "PATCH"
    assert call.path == "pods/pod-1"
    assert call.json_body == {"env": {"FOO": "bar"}}


async def test_update_pod_sync_ports_only(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "PATCH",
        "pods/pod-1",
        {"id": "pod-1", "desiredStatus": "RUNNING"},
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    result = await pods.update_pod("pod-1", ports=["8000/http", "8080/tcp"])

    assert result["id"] == "pod-1"
    call = runpod_rest_fake.calls[0]
    assert call.json_body == {"ports": ["8000/http", "8080/tcp"]}


async def test_update_pod_sync_all_fields(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "PATCH",
        "pods/pod-1",
        {"id": "pod-1", "desiredStatus": "RUNNING"},
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    result = await pods.update_pod(
        "pod-1",
        env={"FOO": "bar"},
        ports=["8000/http"],
        container_registry_auth_id="auth-123",
    )

    assert result["id"] == "pod-1"
    call = runpod_rest_fake.calls[0]
    assert call.json_body == {
        "env": {"FOO": "bar"},
        "ports": ["8000/http"],
        "containerRegistryAuthId": "auth-123",
    }


async def test_update_pod_sync_requires_at_least_one_field(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    with pytest.raises(pods.RunPodError, match="at least one field"):
        await pods.update_pod("pod-1")


async def test_update_pod_wraps_rest_error(
    monkeypatch: pytest.MonkeyPatch,
    runpod_rest_fake: RunPodRestFake,
) -> None:
    runpod_rest_fake.add(
        "PATCH",
        "pods/pod-1",
        pods.RunPodRestError("PATCH", "pods/pod-1", 422, "unprocessable entity"),
    )

    monkeypatch.setattr(pods, "_rest_request", runpod_rest_fake)

    with pytest.raises(pods.RunPodError, match="update_pod"):
        await pods.update_pod("pod-1", env={"FOO": "bar"})
