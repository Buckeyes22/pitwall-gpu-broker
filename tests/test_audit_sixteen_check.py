"""Hermetic tests for the RunPod audit harness.

Lock audit CLI contract. Every check is exercised with both
passing and failing synthetic configurations. No live RunPod calls.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from pitwall.audit import sixteen_check
from pitwall.audit.sixteen_check import (
    CANONICAL_GPU_NAMES,
    CHECK_DESCRIPTIONS,
    CHECK_FUNCTIONS,
    EXPECTED_AUDIT_CHECK_COUNT,
    CheckFailed,
    format_report,
    run_all_checks,
)


def _vllm_provider_fixture(
    *,
    provider_type: str = "serverless_lb",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "prov_custom_qwen3_vllm_fixture",
        "capability_id": "cap_llm_qwen3_32b",
        "name": "qwen3-32b-vllm-fixture",
        "provider_type": provider_type,
        "runpod_endpoint_id": "qwen3-32b-fixture",
        "config": config
        or {
            "gpu_type": "NVIDIA L4",
            "lb_base_url": "https://qwen3-32b-fixture.api.runpod.ai",
            "image_ref": "ghcr.io/acme/pitwall/operator-vllm:fixture",
            "container_disk_gb": 80,
            "workers": {
                "workers_min": 0,
                "workers_max": 1,
            },
            "env_vars": {
                "VLLM_MODEL": "Qwen/Qwen3-32B",
            },
            "worker_image": {
                "download_command": "hf download ${VLLM_MODEL}",
            },
        },
    }


def _pod_lease_provider_fixture(
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "prov_embedding_pod_lease_fixture",
        "capability_id": "cap_embedding_bge_m3",
        "name": "bge-m3-pod-lease-fixture",
        "provider_type": "pod_lease",
        "region": "US-KS-2",
        "config": config
        or {
            "image_ref": "ghcr.io/acme/pitwall/worker-embed:fixture",
            "template_name": "pitwall-bge-m3-pod-lease",
            "gpu_type_priority": ["NVIDIA L4"],
            "container_disk_gb": 40,
            "ports": {"http": [8000], "tcp": [22]},
            "volume_id": "vol-model-cache",
            "data_center_id": "US-KS-2",
            "constraints": {
                "max_attach_hang_s": 300,
                "max_cost_per_hr": 1.0,
            },
            "readiness": {
                "required_signals": ["runtime", "port_mappings", "probe_2xx"],
            },
            "cost_check_order": ["cost_cap", "readiness_wait"],
            "r2": {
                "required": True,
                "credential_strategy": "temp_credentials",
            },
        },
    }


class DictAuditConfig:
    """Test double implementing AuditConfig from plain dicts."""

    def __init__(
        self,
        *,
        gpu_ids: list[str] | None = None,
        workloads: list[dict[str, Any]] | None = None,
        launch_params: dict[str, Any] | None = None,
        readiness_config: dict[str, Any] | None = None,
        cost_config: dict[str, Any] | None = None,
        timeout_config: dict[str, Any] | None = None,
        webhook_config: dict[str, Any] | None = None,
        retention_config: dict[str, Any] | None = None,
        volume_config: dict[str, Any] | None = None,
        probe_config: dict[str, Any] | None = None,
        image_config: dict[str, Any] | None = None,
        disk_config: dict[str, Any] | None = None,
        template_config: dict[str, Any] | None = None,
        registry_config: dict[str, Any] | None = None,
        pre_spend_payloads: list[dict[str, Any]] | None = None,
        provider_fixtures: list[Any] | None = None,
        terminate_config: dict[str, Any] | None = None,
        kill_switch_config: dict[str, Any] | None = None,
    ) -> None:
        self._store: dict[str, Any] = {}
        self._gpu_ids = gpu_ids or ["NVIDIA H100 80GB HBM3", "NVIDIA L4"]
        self._workloads = workloads or [
            {"name": "vllm", "disk_gb": 80},
        ]
        self._launch_params = launch_params or {
            "cloud_type": "SECURE",
            "networkVolumeId": "",
        }
        self._readiness_config = readiness_config or {"probe_field": "runtime"}
        self._cost_config = cost_config or {
            "check_order": ["cost", "readiness"],
        }
        self._timeout_config = timeout_config or {
            "executionTimeout": 3600,
            "executionTimeoutMax": 7200,
            "ttl": 7200,
            "expected_queue_time": 300,
        }
        self._webhook_config = webhook_config or {
            "idempotent": True,
            "fast_200": True,
        }
        self._retention_config = retention_config or {
            "sync_retention_s": 60,
            "async_retention_s": 1800,
            "persist_before_expiry": True,
            "sync_persist_deadline_s": 30,
            "async_persist_deadline_s": 300,
        }
        self._volume_config = volume_config or {
            "networkVolumeId": "vol-123",
            "dataCenterIds": ["DC-1"],
        }
        self._probe_config = probe_config or {
            "ssh_first": True,
            "probe_methods": ["ssh_localhost", "runpod_proxy"],
            "primary_probe": "ssh_localhost",
        }
        self._image_config = image_config or {
            "image_pull_timeout_s": 600,
            "startup_timeout_s": 600,
        }
        self._disk_config = disk_config or {
            "per_workload": {"vllm": 80, "embed": 40, "slim": 20},
        }
        self._template_config = template_config or {
            "cache_enabled": True,
            "create_on_cache_miss": True,
            "reuse_on_cache_hit": True,
        }
        self._registry_config = registry_config or {
            "prefix_to_auth_id": {
                "ghcr.io": "auth-ghcr",
                "registry.gitlab.com": "auth-gitlab",
                "docker.io": None,
            },
        }
        self._pre_spend_payloads = pre_spend_payloads or [
            {"texts": ["hello world"], "metadata": {"tenant": "audit-fixture"}},
        ]
        self._provider_fixtures = provider_fixtures or [
            _vllm_provider_fixture(),
            _pod_lease_provider_fixture(),
        ]
        self._terminate_config = terminate_config or {
            "treat_404_as_success": True,
        }
        self._kill_switch_config = kill_switch_config or {
            "atomic": True,
            "steps": ["list_pods", "terminate_all", "verify"],
            "budget_s": 25,
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def gpu_ids(self) -> list[str]:
        return self._gpu_ids

    def workloads(self) -> list[dict[str, Any]]:
        return self._workloads

    def launch_params(self) -> dict[str, Any]:
        return self._launch_params

    def readiness_config(self) -> dict[str, Any]:
        return self._readiness_config

    def cost_config(self) -> dict[str, Any]:
        return self._cost_config

    def timeout_config(self) -> dict[str, Any]:
        return self._timeout_config

    def webhook_config(self) -> dict[str, Any]:
        return self._webhook_config

    def retention_config(self) -> dict[str, Any]:
        return self._retention_config

    def volume_config(self) -> dict[str, Any]:
        return self._volume_config

    def probe_config(self) -> dict[str, Any]:
        return self._probe_config

    def image_config(self) -> dict[str, Any]:
        return self._image_config

    def disk_config(self) -> dict[str, Any]:
        return self._disk_config

    def template_config(self) -> dict[str, Any]:
        return self._template_config

    def registry_config(self) -> dict[str, Any]:
        return self._registry_config

    def pre_spend_payloads(self) -> list[dict[str, Any]]:
        return self._pre_spend_payloads

    def provider_fixtures(self) -> list[Any]:
        return self._provider_fixtures

    def terminate_config(self) -> dict[str, Any]:
        return self._terminate_config

    def kill_switch_config(self) -> dict[str, Any]:
        return self._kill_switch_config


def _passing_config(**overrides: Any) -> DictAuditConfig:
    return DictAuditConfig(**overrides)


class TestSixteenCheckCount:
    def test_exactly_expected_check_functions(self):
        assert len(CHECK_FUNCTIONS) == EXPECTED_AUDIT_CHECK_COUNT

    def test_all_check_ids_present(self):
        ids = {int(fn.__name__.split("_")[1]) for fn in CHECK_FUNCTIONS}
        assert ids == set(range(1, EXPECTED_AUDIT_CHECK_COUNT + 1))

    def test_all_checks_have_descriptions(self):
        for i in range(1, EXPECTED_AUDIT_CHECK_COUNT + 1):
            assert i in CHECK_DESCRIPTIONS


class TestRunAllChecksPassing:
    def test_all_pass_with_valid_config(self):
        cfg = _passing_config()
        results = run_all_checks(cfg)
        assert len(results) == EXPECTED_AUDIT_CHECK_COUNT
        assert all(r.passed for r in results), "failing checks: " + ", ".join(
            f"#{r.check_id}: {r.message}" for r in results if not r.passed
        )


class TestCheck01GpuIdsCanonical:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_01_gpu_ids_canonical

        cfg = _passing_config(gpu_ids=["NVIDIA H100 80GB HBM3", "NVIDIA L4"])
        assert "canonical" in check_01_gpu_ids_canonical(cfg)

    def test_fail_non_canonical(self):
        from pitwall.audit.sixteen_check import check_01_gpu_ids_canonical

        cfg = _passing_config(gpu_ids=["H100", "L4"])
        with pytest.raises(CheckFailed) as exc_info:
            check_01_gpu_ids_canonical(cfg)
        assert exc_info.value.check_id == 1

    def test_canonical_set_not_empty(self):
        assert len(CANONICAL_GPU_NAMES) > 0


class TestCheck02CloudTypeVolume:
    def test_pass_secure_with_volume(self):
        from pitwall.audit.sixteen_check import check_02_cloud_type_volume

        cfg = _passing_config(
            launch_params={"cloud_type": "SECURE", "networkVolumeId": "vol-1"},
        )
        check_02_cloud_type_volume(cfg)

    def test_pass_all_without_volume(self):
        from pitwall.audit.sixteen_check import check_02_cloud_type_volume

        cfg = _passing_config(
            launch_params={"cloud_type": "ALL", "networkVolumeId": ""},
        )
        check_02_cloud_type_volume(cfg)

    def test_fail_all_with_volume(self):
        from pitwall.audit.sixteen_check import check_02_cloud_type_volume

        cfg = _passing_config(
            launch_params={"cloud_type": "ALL", "networkVolumeId": "vol-1"},
        )
        with pytest.raises(CheckFailed) as exc_info:
            check_02_cloud_type_volume(cfg)
        assert exc_info.value.check_id == 2


class TestCheck03ReadinessRuntime:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_03_readiness_runtime

        cfg = _passing_config(readiness_config={"probe_field": "runtime"})
        check_03_readiness_runtime(cfg)

    def test_fail_desired_status(self):
        from pitwall.audit.sixteen_check import check_03_readiness_runtime

        cfg = _passing_config(readiness_config={"probe_field": "desiredStatus"})
        with pytest.raises(CheckFailed) as exc_info:
            check_03_readiness_runtime(cfg)
        assert exc_info.value.check_id == 3

    def test_fail_pod_lease_fixture_without_probe_signal(self):
        from pitwall.audit.sixteen_check import check_03_readiness_runtime

        config = dict(_pod_lease_provider_fixture()["config"])
        config["readiness"] = {"required_signals": ["runtime", "port_mappings"]}
        cfg = _passing_config(provider_fixtures=[_pod_lease_provider_fixture(config=config)])

        with pytest.raises(CheckFailed) as exc_info:
            check_03_readiness_runtime(cfg)

        assert exc_info.value.check_id == 3
        assert "probe_2xx" in exc_info.value.message


class TestCheck04CostCapBeforeReadiness:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_04_cost_cap_before_readiness

        cfg = _passing_config(
            cost_config={"check_order": ["cost", "readiness"]},
        )
        check_04_cost_cap_before_readiness(cfg)

    def test_fail_cost_after_readiness(self):
        from pitwall.audit.sixteen_check import check_04_cost_cap_before_readiness

        cfg = _passing_config(
            cost_config={"check_order": ["readiness", "cost"]},
        )
        with pytest.raises(CheckFailed) as exc_info:
            check_04_cost_cap_before_readiness(cfg)
        assert exc_info.value.check_id == 4

    def test_fail_no_cost(self):
        from pitwall.audit.sixteen_check import check_04_cost_cap_before_readiness

        cfg = _passing_config(cost_config={"check_order": ["readiness"]})
        with pytest.raises(CheckFailed):
            check_04_cost_cap_before_readiness(cfg)

    def test_fail_pod_lease_fixture_cost_after_readiness(self):
        from pitwall.audit.sixteen_check import check_04_cost_cap_before_readiness

        config = dict(_pod_lease_provider_fixture()["config"])
        config["cost_check_order"] = ["readiness_wait", "cost_cap"]
        cfg = _passing_config(provider_fixtures=[_pod_lease_provider_fixture(config=config)])

        with pytest.raises(CheckFailed) as exc_info:
            check_04_cost_cap_before_readiness(cfg)

        assert exc_info.value.check_id == 4
        assert "after readiness" in exc_info.value.message


class TestCheck05ExecutionTimeout:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_05_execution_timeout

        cfg = _passing_config(
            timeout_config={
                "executionTimeout": 3600,
                "executionTimeoutMax": 7200,
                "ttl": 7200,
                "expected_queue_time": 300,
            },
        )
        check_05_execution_timeout(cfg)

    def test_fail_not_set(self):
        from pitwall.audit.sixteen_check import check_05_execution_timeout

        cfg = _passing_config(
            timeout_config={
                "executionTimeoutMax": 7200,
                "ttl": 7200,
                "expected_queue_time": 300,
            },
        )
        with pytest.raises(CheckFailed) as exc_info:
            check_05_execution_timeout(cfg)
        assert exc_info.value.check_id == 5

    def test_fail_exceeds_max(self):
        from pitwall.audit.sixteen_check import check_05_execution_timeout

        cfg = _passing_config(
            timeout_config={
                "executionTimeout": 8000,
                "executionTimeoutMax": 7200,
                "ttl": 10000,
                "expected_queue_time": 300,
            },
        )
        with pytest.raises(CheckFailed):
            check_05_execution_timeout(cfg)


class TestCheck06TtlGeTimeout:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_06_ttl_ge_timeout_plus_queue

        cfg = _passing_config(
            timeout_config={
                "executionTimeout": 3600,
                "ttl": 7200,
                "expected_queue_time": 300,
            },
        )
        check_06_ttl_ge_timeout_plus_queue(cfg)

    def test_fail_ttl_too_small(self):
        from pitwall.audit.sixteen_check import check_06_ttl_ge_timeout_plus_queue

        cfg = _passing_config(
            timeout_config={
                "executionTimeout": 3600,
                "ttl": 3000,
                "expected_queue_time": 300,
            },
        )
        with pytest.raises(CheckFailed) as exc_info:
            check_06_ttl_ge_timeout_plus_queue(cfg)
        assert exc_info.value.check_id == 6


class TestCheck07Webhook:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_07_webhook_idempotent_fast200

        cfg = _passing_config(
            webhook_config={"idempotent": True, "fast_200": True},
        )
        check_07_webhook_idempotent_fast200(cfg)

    def test_fail_not_idempotent(self):
        from pitwall.audit.sixteen_check import check_07_webhook_idempotent_fast200

        cfg = _passing_config(
            webhook_config={"idempotent": False, "fast_200": True},
        )
        with pytest.raises(CheckFailed) as exc_info:
            check_07_webhook_idempotent_fast200(cfg)
        assert exc_info.value.check_id == 7

    def test_fail_no_fast_200(self):
        from pitwall.audit.sixteen_check import check_07_webhook_idempotent_fast200

        cfg = _passing_config(
            webhook_config={"idempotent": True, "fast_200": False},
        )
        with pytest.raises(CheckFailed):
            check_07_webhook_idempotent_fast200(cfg)


class TestCheck08Retention:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_08_retention_windows

        cfg = _passing_config(
            retention_config={
                "sync_retention_s": 60,
                "async_retention_s": 1800,
                "persist_before_expiry": True,
                "sync_persist_deadline_s": 30,
                "async_persist_deadline_s": 300,
            },
        )
        check_08_retention_windows(cfg)

    def test_fail_sync_too_short(self):
        from pitwall.audit.sixteen_check import check_08_retention_windows

        cfg = _passing_config(
            retention_config={"sync_retention_s": 30, "async_retention_s": 1800},
        )
        with pytest.raises(CheckFailed) as exc_info:
            check_08_retention_windows(cfg)
        assert exc_info.value.check_id == 8

    def test_fail_async_too_short(self):
        from pitwall.audit.sixteen_check import check_08_retention_windows

        cfg = _passing_config(
            retention_config={"sync_retention_s": 60, "async_retention_s": 600},
        )
        with pytest.raises(CheckFailed):
            check_08_retention_windows(cfg)


class TestCheck09DcPin:
    def test_pass_single_dc(self):
        from pitwall.audit.sixteen_check import check_09_dc_pin

        cfg = _passing_config(
            volume_config={
                "networkVolumeId": "vol-1",
                "dataCenterIds": ["DC-1"],
            },
        )
        check_09_dc_pin(cfg)

    def test_pass_no_volume(self):
        from pitwall.audit.sixteen_check import check_09_dc_pin

        cfg = _passing_config(
            volume_config={"networkVolumeId": "", "dataCenterIds": []},
        )
        check_09_dc_pin(cfg)

    def test_fail_multi_dc(self):
        from pitwall.audit.sixteen_check import check_09_dc_pin

        cfg = _passing_config(
            volume_config={
                "networkVolumeId": "vol-1",
                "dataCenterIds": ["DC-1", "DC-2"],
            },
        )
        with pytest.raises(CheckFailed) as exc_info:
            check_09_dc_pin(cfg)
        assert exc_info.value.check_id == 9

    def test_fail_pod_lease_attach_timeout_over_five_minutes(self):
        from pitwall.audit.sixteen_check import check_09_dc_pin

        config = dict(_pod_lease_provider_fixture()["config"])
        constraints = dict(config["constraints"])
        constraints["max_attach_hang_s"] = 301
        config["constraints"] = constraints
        cfg = _passing_config(provider_fixtures=[_pod_lease_provider_fixture(config=config)])

        with pytest.raises(CheckFailed) as exc_info:
            check_09_dc_pin(cfg)

        assert exc_info.value.check_id == 9
        assert "exceeds 300s" in exc_info.value.message


class TestCheck10SshFirst:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_10_ssh_first_probe

        cfg = _passing_config(probe_config={"ssh_first": True})
        check_10_ssh_first_probe(cfg)

    def test_fail(self):
        from pitwall.audit.sixteen_check import check_10_ssh_first_probe

        cfg = _passing_config(probe_config={"ssh_first": False})
        with pytest.raises(CheckFailed) as exc_info:
            check_10_ssh_first_probe(cfg)
        assert exc_info.value.check_id == 10


class TestCheck11ImagePullTimeout:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_11_image_pull_timeout

        cfg = _passing_config(
            image_config={"image_pull_timeout_s": 300, "startup_timeout_s": 300},
        )
        check_11_image_pull_timeout(cfg)

    def test_fail_not_set(self):
        from pitwall.audit.sixteen_check import check_11_image_pull_timeout

        cfg = _passing_config(
            image_config={"startup_timeout_s": 300},
        )
        with pytest.raises(CheckFailed) as exc_info:
            check_11_image_pull_timeout(cfg)
        assert exc_info.value.check_id == 11

    def test_fail_too_small(self):
        from pitwall.audit.sixteen_check import check_11_image_pull_timeout

        cfg = _passing_config(
            image_config={"image_pull_timeout_s": 60, "startup_timeout_s": 300},
        )
        with pytest.raises(CheckFailed):
            check_11_image_pull_timeout(cfg)

    def test_fail_pod_lease_long_lived_r2_strategy(self):
        from pitwall.audit.sixteen_check import check_11_image_pull_timeout

        config = dict(_pod_lease_provider_fixture()["config"])
        config["r2"] = {"required": True, "credential_strategy": "long_lived"}
        cfg = _passing_config(provider_fixtures=[_pod_lease_provider_fixture(config=config)])

        with pytest.raises(CheckFailed) as exc_info:
            check_11_image_pull_timeout(cfg)

        assert exc_info.value.check_id == 11
        assert "non-temporary" in exc_info.value.message

    def test_fail_pod_lease_static_r2_env_injection(self):
        from pitwall.audit.sixteen_check import check_11_image_pull_timeout

        config = dict(_pod_lease_provider_fixture()["config"])
        config["env_vars"] = {"R2_ACCESS_KEY": "static-access"}
        cfg = _passing_config(provider_fixtures=[_pod_lease_provider_fixture(config=config)])

        with pytest.raises(CheckFailed) as exc_info:
            check_11_image_pull_timeout(cfg)

        assert exc_info.value.check_id == 11
        assert "R2 credential env keys" in exc_info.value.message

    def test_passes_with_staging_store_noop_default(self, monkeypatch: pytest.MonkeyPatch):
        from pitwall.audit.sixteen_check import check_11_image_pull_timeout

        for key in (
            "R2_ENDPOINT",
            "R2_BUCKET_STAGING",
            "R2_BUCKET",
            "CLOUDFLARE_ACCOUNT_ID",
            "CF_ACCOUNT_ID",
            "R2_ACCOUNT_ID",
            "CLOUDFLARE_API_TOKEN",
            "CF_API_TOKEN",
            "R2_TEMP_CREDENTIAL_API_TOKEN",
            "R2_PARENT_ACCESS_KEY_ID",
            "CLOUDFLARE_R2_PARENT_ACCESS_KEY_ID",
            "R2_ACCESS_KEY_ID",
            "R2_ACCESS_KEY",
            "R2_SECRET_KEY",
            "R2_TEMP_CREDENTIALS_ENABLED",
            "PITWALL_R2_TEMP_CREDENTIALS_ENABLED",
            "R2_TEMP_CREDENTIALS_REQUIRED",
            "PITWALL_R2_TEMP_CREDENTIALS_REQUIRED",
        ):
            monkeypatch.delenv(key, raising=False)

        cfg = _passing_config(
            image_config={"image_pull_timeout_s": 300, "startup_timeout_s": 300},
        )

        assert "StagingStore" in check_11_image_pull_timeout(cfg)


class TestCheck12DiskSized:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_12_disk_sized

        cfg = _passing_config(
            disk_config={"per_workload": {"vllm": 80, "embed": 40, "slim": 20}},
        )
        check_12_disk_sized(cfg)

    def test_pass_with_vllm_provider_fixture(self):
        from pitwall.audit.sixteen_check import check_12_disk_sized

        cfg = _passing_config(provider_fixtures=[_vllm_provider_fixture()])
        check_12_disk_sized(cfg)

    def test_fail_empty(self):
        from pitwall.audit.sixteen_check import check_12_disk_sized

        cfg = _passing_config(disk_config={"per_workload": {}})
        with pytest.raises(CheckFailed) as exc_info:
            check_12_disk_sized(cfg)
        assert exc_info.value.check_id == 12

    def test_fail_zero_size(self):
        from pitwall.audit.sixteen_check import check_12_disk_sized

        cfg = _passing_config(
            disk_config={"per_workload": {"vllm": 0}},
        )
        with pytest.raises(CheckFailed):
            check_12_disk_sized(cfg)

    def test_fail_vllm_provider_disk_too_small(self):
        from pitwall.audit.sixteen_check import check_12_disk_sized

        provider = _vllm_provider_fixture(
            config={
                "image_ref": "ghcr.io/acme/pitwall/operator-vllm:fixture",
                "container_disk_gb": 40,
                "workers": {"workers_min": 0},
                "env_vars": {"VLLM_MODEL": "Qwen/Qwen3-32B"},
                "worker_image": {"download_command": "hf download ${VLLM_MODEL}"},
            },
        )
        cfg = _passing_config(provider_fixtures=[provider])
        with pytest.raises(CheckFailed) as exc_info:
            check_12_disk_sized(cfg)
        assert exc_info.value.check_id == 12
        assert "disk" in exc_info.value.message

    def test_fail_vllm_provider_deprecated_hf_cli(self):
        from pitwall.audit.sixteen_check import check_12_disk_sized

        deprecated_cmd = " ".join(("huggingface-cli", "download"))
        provider = _vllm_provider_fixture(
            config={
                "image_ref": "ghcr.io/acme/pitwall/operator-vllm:fixture",
                "container_disk_gb": 80,
                "workers": {"workers_min": 0},
                "env_vars": {"VLLM_MODEL": "Qwen/Qwen3-32B"},
                "worker_image": {
                    "download_command": f"{deprecated_cmd} ${{VLLM_MODEL}}",
                },
            },
        )
        cfg = _passing_config(provider_fixtures=[provider])
        with pytest.raises(CheckFailed) as exc_info:
            check_12_disk_sized(cfg)
        assert exc_info.value.check_id == 12
        assert deprecated_cmd in exc_info.value.message


class TestCheck13TemplateCache:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_13_template_cache

        cfg = _passing_config(template_config={"cache_enabled": True})
        check_13_template_cache(cfg)

    def test_fail(self):
        from pitwall.audit.sixteen_check import check_13_template_cache

        cfg = _passing_config(template_config={"cache_enabled": False})
        with pytest.raises(CheckFailed) as exc_info:
            check_13_template_cache(cfg)
        assert exc_info.value.check_id == 13


class TestCheck14RegistryAuth:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_14_registry_auth

        cfg = _passing_config(
            registry_config={
                "prefix_to_auth_id": {
                    "ghcr.io": "auth-ghcr",
                    "registry.gitlab.com": "auth-gitlab",
                    "docker.io": None,
                },
            },
        )
        check_14_registry_auth(cfg)

    def test_fail_empty(self):
        from pitwall.audit.sixteen_check import check_14_registry_auth

        cfg = _passing_config(registry_config={"prefix_to_auth_id": {}})
        with pytest.raises(CheckFailed) as exc_info:
            check_14_registry_auth(cfg)
        assert exc_info.value.check_id == 14

    def test_fail_vllm_provider_workers_min_positive(self):
        from pitwall.audit.sixteen_check import check_14_registry_auth

        provider = _vllm_provider_fixture(
            config={
                "image_ref": "ghcr.io/acme/pitwall/operator-vllm:fixture",
                "container_disk_gb": 80,
                "workers": {"workers_min": 1},
                "env_vars": {"VLLM_MODEL": "Qwen/Qwen3-32B"},
                "worker_image": {"download_command": "hf download ${VLLM_MODEL}"},
            },
        )
        cfg = _passing_config(provider_fixtures=[provider])
        with pytest.raises(CheckFailed) as exc_info:
            check_14_registry_auth(cfg)
        assert exc_info.value.check_id == 14
        assert "workers_min=1" in exc_info.value.message


class TestCheck15TerminateIdempotent:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_15_terminate_idempotent

        cfg = _passing_config(
            terminate_config={"treat_404_as_success": True},
        )
        check_15_terminate_idempotent(cfg)

    def test_fail(self):
        from pitwall.audit.sixteen_check import check_15_terminate_idempotent

        cfg = _passing_config(
            terminate_config={"treat_404_as_success": False},
        )
        with pytest.raises(CheckFailed) as exc_info:
            check_15_terminate_idempotent(cfg)
        assert exc_info.value.check_id == 15

    def test_fail_missing_single_lease_stop_route(self, monkeypatch: pytest.MonkeyPatch):
        from pitwall.api.routes import leases as lease_routes
        from pitwall.audit.sixteen_check import check_15_terminate_idempotent

        remaining_routes = [
            route
            for route in lease_routes.router.routes
            if getattr(route, "path", None) != "/v1/leases/{lease_id}/stop"
        ]
        monkeypatch.setattr(lease_routes.router, "routes", remaining_routes)

        cfg = _passing_config(terminate_config={"treat_404_as_success": True})
        with pytest.raises(CheckFailed) as exc_info:
            check_15_terminate_idempotent(cfg)

        assert exc_info.value.check_id == 15
        assert "single-lease stop route" in exc_info.value.message


class TestCheck16KillSwitch:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_16_kill_switch_atomic

        cfg = _passing_config(
            kill_switch_config={
                "atomic": True,
                "steps": ["list_pods", "terminate_all", "verify"],
                "budget_s": 25,
            },
        )
        check_16_kill_switch_atomic(cfg)

    def test_fail_not_atomic(self):
        from pitwall.audit.sixteen_check import check_16_kill_switch_atomic

        cfg = _passing_config(
            kill_switch_config={
                "atomic": False,
                "steps": ["list_pods", "terminate_all", "verify"],
                "budget_s": 25,
            },
        )
        with pytest.raises(CheckFailed) as exc_info:
            check_16_kill_switch_atomic(cfg)
        assert exc_info.value.check_id == 16

    def test_fail_wrong_step_count(self):
        from pitwall.audit.sixteen_check import check_16_kill_switch_atomic

        cfg = _passing_config(
            kill_switch_config={
                "atomic": True,
                "steps": ["list_pods", "terminate_all"],
                "budget_s": 25,
            },
        )
        with pytest.raises(CheckFailed):
            check_16_kill_switch_atomic(cfg)

    def test_fail_budget_exceeded(self):
        from pitwall.audit.sixteen_check import check_16_kill_switch_atomic

        cfg = _passing_config(
            kill_switch_config={
                "atomic": True,
                "steps": ["list_pods", "terminate_all", "verify"],
                "budget_s": 60,
            },
        )
        with pytest.raises(CheckFailed):
            check_16_kill_switch_atomic(cfg)

    def test_fail_when_patch_validator_does_not_reject_multi_axis(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from pitwall.api.schemas import leases as lease_schemas
        from pitwall.audit.sixteen_check import check_16_kill_switch_atomic

        monkeypatch.setattr(lease_schemas, "lease_patch_conflicting_fields", lambda _payload: [])
        cfg = _passing_config()

        with pytest.raises(CheckFailed) as exc_info:
            check_16_kill_switch_atomic(cfg)

        assert exc_info.value.check_id == 16
        assert "multi-axis validation" in exc_info.value.message


class TestPreSpendPayloadScanner:
    def test_allows_benign_payload_without_redaction(self):
        result = sixteen_check.scan_pre_spend_payload(
            {"texts": ["hello world"], "metadata": {"tenant": "fixture"}}
        )

        assert result.decision == sixteen_check.PreSpendDecision.ALLOW
        assert result.findings == ()
        assert result.redacted_payload == {
            "metadata": {"tenant": "fixture"},
            "texts": ["hello world"],
        }
        assert result.to_dict() == {
            "decision": "allow",
            "blocked": False,
            "findings": [],
            "redacted_payload": {
                "metadata": {"tenant": "fixture"},
                "texts": ["hello world"],
            },
        }

    def test_blocks_api_key_shape_and_redacts_finding_preview(self):
        token = "sk-test_1234567890abcdef1234567890abcdef"
        result = sixteen_check.scan_pre_spend_payload(
            {"messages": [{"content": f"use token {token} now"}]}
        )

        assert result.decision == sixteen_check.PreSpendDecision.BLOCK
        assert result.blocked is True
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.kind == sixteen_check.PreSpendFindingKind.SECRET
        assert finding.action == sixteen_check.PreSpendDecision.BLOCK
        assert finding.path == "$.messages[0].content"
        assert token not in finding.redacted_preview
        assert token not in str(result.to_dict())
        assert result.redacted_payload == {
            "messages": [{"content": "use token [REDACTED:secret] now"}]
        }

    def test_blocks_private_key_material(self):
        private_key = "\n".join(
            (
                "-----BEGIN PRIVATE KEY-----",
                "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
                "-----END PRIVATE KEY-----",
            )
        )

        result = sixteen_check.scan_pre_spend_payload({"prompt": private_key})

        assert result.decision == sixteen_check.PreSpendDecision.BLOCK
        assert result.findings[0].rule == "private_key"
        assert private_key not in str(result.to_dict())

    def test_redacts_email_pii_without_blocking(self):
        result = sixteen_check.scan_pre_spend_payload(
            {"texts": ["please contact ada.lovelace@example.com about the ticket"]}
        )

        assert result.decision == sixteen_check.PreSpendDecision.REDACT
        assert result.blocked is False
        assert result.findings[0].kind == sixteen_check.PreSpendFindingKind.PII
        assert result.findings[0].action == sixteen_check.PreSpendDecision.REDACT
        assert result.redacted_payload == {
            "texts": ["please contact [REDACTED:email] about the ticket"]
        }
        assert "ada.lovelace@example.com" not in str(result.to_dict())

    def test_secret_like_control_fields_are_not_false_positives(self):
        result = sixteen_check.scan_pre_spend_payload(
            {
                "capability_id": "embedding.bge-m3",
                "provider_id": "prov_bge_m3",
                "idempotency_key": "idem_1234567890abcdef",
            }
        )

        assert result.decision == sixteen_check.PreSpendDecision.ALLOW

    def test_block_decision_survives_finding_cap_after_pii_redactions(self):
        secret = "sk-test_1234567890abcdef1234567890abcdef"
        payload = {
            "texts": [
                *(f"contact user{index}@example.com" for index in range(32)),
                f"use token {secret}",
            ]
        }

        result = sixteen_check.scan_pre_spend_payload(payload)

        assert result.decision == sixteen_check.PreSpendDecision.BLOCK
        assert result.blocked is True
        assert len(result.findings) == 32
        assert all(
            finding.kind == sixteen_check.PreSpendFindingKind.PII for finding in result.findings
        )
        assert result.redacted_payload["texts"][-1] == "use token [REDACTED:secret]"
        assert secret not in str(result.to_dict())

    @pytest.mark.parametrize(
        ("payload", "secret"),
        [
            (
                {"prompt": ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")},
                "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            ),
            (
                {"texts": [("R2_SECRET_ACCESS_KEY=r2-secret-access-key-material-1234567890")]},
                "r2-secret-access-key-material-1234567890",
            ),
            (
                {"prompt": "R2_SECRET_KEY: r2-secret-key-material-1234567890"},
                "r2-secret-key-material-1234567890",
            ),
            (
                {"texts": ["AWS_SESSION_TOKEN=aws-session-token-material-1234567890"]},
                "aws-session-token-material-1234567890",
            ),
        ],
    )
    def test_blocks_labeled_cloud_secret_assignments_in_prompt_and_texts(
        self,
        payload: dict[str, object],
        secret: str,
    ):
        result = sixteen_check.scan_pre_spend_payload(payload)

        assert result.decision == sixteen_check.PreSpendDecision.BLOCK
        assert result.blocked is True
        assert result.findings[0].rule == "labeled_cloud_secret_assignment"
        assert secret not in str(result.to_dict())
        assert secret not in str(result.redacted_payload)


class TestCheck17PreSpendSecrets:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_17_pre_spend_secret_guardrail

        cfg = _passing_config(pre_spend_payloads=[{"texts": ["ordinary request"]}])
        assert "secret" in check_17_pre_spend_secret_guardrail(cfg)

    def test_fail_configured_payload_contains_secret(self):
        from pitwall.audit.sixteen_check import check_17_pre_spend_secret_guardrail

        cfg = _passing_config(
            pre_spend_payloads=[{"prompt": "token=sk-test_1234567890abcdef1234567890abcdef"}],
        )

        with pytest.raises(CheckFailed) as exc_info:
            check_17_pre_spend_secret_guardrail(cfg)

        assert exc_info.value.check_id == 17
        assert "secret" in exc_info.value.message
        assert "sk-test" not in exc_info.value.evidence

    def test_fail_configured_payload_blocks_secret_after_finding_cap(self):
        from pitwall.audit.sixteen_check import check_17_pre_spend_secret_guardrail

        cfg = _passing_config(
            pre_spend_payloads=[
                {
                    "texts": [
                        *(f"contact user{index}@example.com" for index in range(32)),
                        "token=sk-test_1234567890abcdef1234567890abcdef",
                    ]
                }
            ],
        )

        with pytest.raises(CheckFailed) as exc_info:
            check_17_pre_spend_secret_guardrail(cfg)

        assert exc_info.value.check_id == 17
        assert "secret" in exc_info.value.message
        assert "sk-test" not in exc_info.value.evidence


class TestCheck18PreSpendPii:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_18_pre_spend_pii_redaction

        cfg = _passing_config(pre_spend_payloads=[{"texts": ["ordinary request"]}])
        assert "PII" in check_18_pre_spend_pii_redaction(cfg)

    def test_fail_configured_payload_contains_unredacted_pii(self):
        from pitwall.audit.sixteen_check import check_18_pre_spend_pii_redaction

        cfg = _passing_config(
            pre_spend_payloads=[{"texts": ["email jane.roe@example.com for approval"]}],
        )

        with pytest.raises(CheckFailed) as exc_info:
            check_18_pre_spend_pii_redaction(cfg)

        assert exc_info.value.check_id == 18
        assert "PII" in exc_info.value.message
        assert "jane.roe@example.com" not in exc_info.value.evidence


class TestCheck19PolicyAsCode:
    def test_pass(self):
        from pitwall.audit.sixteen_check import check_19_policy_as_code_audit_gate

        cfg = _passing_config()
        assert "policy" in check_19_policy_as_code_audit_gate(cfg)

    def test_fail_provider_fixture_violates_policy(self):
        from pitwall.audit.sixteen_check import check_19_policy_as_code_audit_gate

        config = dict(_pod_lease_provider_fixture()["config"])
        config["env_vars"] = {"R2_ACCESS_KEY": "raw-secret"}
        cfg = _passing_config(provider_fixtures=[_pod_lease_provider_fixture(config=config)])

        with pytest.raises(CheckFailed) as exc_info:
            check_19_policy_as_code_audit_gate(cfg)

        assert exc_info.value.check_id == 19
        assert "policy" in exc_info.value.message
        assert "provider.no-static-r2-env" in exc_info.value.evidence
        assert "raw-secret" not in exc_info.value.evidence


class TestFormatReport:
    def test_report_contains_all_checks(self):
        cfg = _passing_config()
        results = run_all_checks(cfg)
        report = format_report(results)
        assert f"{EXPECTED_AUDIT_CHECK_COUNT}/{EXPECTED_AUDIT_CHECK_COUNT} passed" in report
        for i in range(1, EXPECTED_AUDIT_CHECK_COUNT + 1):
            assert f"[{i:2d}]" in report

    def test_report_shows_failures(self):
        cfg = _passing_config(
            gpu_ids=["BAD_GPU"],
        )
        results = run_all_checks(cfg)
        report = format_report(results)
        assert "FAIL" in report


class TestCLI:
    def test_cli_exit_zero_on_pass(self):
        result = subprocess.run(
            [sys.executable, "-m", "pitwall.audit.sixteen_check"],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": "src",
            },
        )
        assert result.returncode == 0
        assert f"{EXPECTED_AUDIT_CHECK_COUNT}/{EXPECTED_AUDIT_CHECK_COUNT} passed" in result.stdout

    def test_cli_json_output(self):
        import json

        result = subprocess.run(
            [sys.executable, "-m", "pitwall.audit.sixteen_check", "--json"],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": "src",
            },
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "all_passed" in data
        assert "strict" in data
        assert "checks" in data
        checks = data["checks"]
        assert len(checks) == EXPECTED_AUDIT_CHECK_COUNT
        assert all(isinstance(d, dict) for d in checks)
        assert all("check_id" in d for d in checks)
        assert all("passed" in d for d in checks)
        assert all(d["passed"] for d in checks)
        assert data["all_passed"] is True
        assert data["strict"] is False

    def test_cli_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "pitwall.audit.sixteen_check", "--help"],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": "src",
            },
        )
        assert result.returncode == 0
        assert f"{EXPECTED_AUDIT_CHECK_COUNT}-check" in result.stdout


class TestSixteenCheckCoverageMatrix:
    _EXPECTED = {
        "check_01_gpu_ids_canonical": TestCheck01GpuIdsCanonical,
        "check_02_cloud_type_volume": TestCheck02CloudTypeVolume,
        "check_03_readiness_runtime": TestCheck03ReadinessRuntime,
        "check_04_cost_cap_before_readiness": TestCheck04CostCapBeforeReadiness,
        "check_05_execution_timeout": TestCheck05ExecutionTimeout,
        "check_06_ttl_ge_timeout_plus_queue": TestCheck06TtlGeTimeout,
        "check_07_webhook_idempotent_fast200": TestCheck07Webhook,
        "check_08_retention_windows": TestCheck08Retention,
        "check_09_dc_pin": TestCheck09DcPin,
        "check_10_ssh_first_probe": TestCheck10SshFirst,
        "check_11_image_pull_timeout": TestCheck11ImagePullTimeout,
        "check_12_disk_sized": TestCheck12DiskSized,
        "check_13_template_cache": TestCheck13TemplateCache,
        "check_14_registry_auth": TestCheck14RegistryAuth,
        "check_15_terminate_idempotent": TestCheck15TerminateIdempotent,
        "check_16_kill_switch_atomic": TestCheck16KillSwitch,
        "check_17_pre_spend_secret_guardrail": TestCheck17PreSpendSecrets,
        "check_18_pre_spend_pii_redaction": TestCheck18PreSpendPii,
        "check_19_policy_as_code_audit_gate": TestCheck19PolicyAsCode,
    }

    def test_every_registered_check_has_expected_test_class(self):
        registered = {fn.__name__: fn.check_id for fn in CHECK_FUNCTIONS}

        assert set(self._EXPECTED) == set(registered)
        for check_name, test_class in self._EXPECTED.items():
            check_id = registered[check_name]
            assert test_class.__name__.startswith(f"TestCheck{check_id:02d}")

    def test_each_check_test_class_has_pass_and_fail_coverage(self):
        for test_class in self._EXPECTED.values():
            method_names = {
                name
                for name, value in vars(test_class).items()
                if callable(value) and name.startswith("test_")
            }
            assert any(name.startswith("test_pass") for name in method_names), test_class
            assert any(name.startswith("test_fail") for name in method_names), test_class

    def test_check_descriptions_cover_exact_check_ids(self):
        assert set(CHECK_DESCRIPTIONS) == set(range(1, EXPECTED_AUDIT_CHECK_COUNT + 1))


class TestStrictModeExitCode:
    _RUNTIME_ENV_KEYS = (
        "PITWALL_AUDIT_GPU_IDS",
        "PITWALL_AUDIT_CLOUD_TYPE",
        "RUNPOD_NETWORK_VOLUME_ID",
        "RUNPOD_DATA_CENTER_ID",
        "PITWALL_AUDIT_EXEC_TIMEOUT_S",
        "PITWALL_AUDIT_EXEC_TIMEOUT_MAX_S",
        "PITWALL_DEFAULT_LEASE_TTL_S",
        "PITWALL_AUDIT_QUEUE_TIME_S",
        "PITWALL_IMAGE_PULL_TIMEOUT_S",
        "PITWALL_AUDIT_STARTUP_TIMEOUT_S",
    )

    class _FailingCheck:
        check_id = 1

        def __call__(self, _cfg: object) -> str:
            raise CheckFailed(
                self.check_id,
                "forced audit failure",
                evidence="FAIL: forced audit failure",
            )

    def _clear_runtime_audit_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in self._RUNTIME_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)

    def test_strict_exit_zero_when_all_checks_pass(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        self._clear_runtime_audit_env(monkeypatch)

        assert sixteen_check.main(["--strict"]) == 0

        captured = capsys.readouterr()
        assert f"{EXPECTED_AUDIT_CHECK_COUNT}/{EXPECTED_AUDIT_CHECK_COUNT} passed" in captured.out

    def test_strict_exit_one_when_a_check_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        self._clear_runtime_audit_env(monkeypatch)
        monkeypatch.setattr(
            sixteen_check,
            "CHECK_FUNCTIONS",
            [self._FailingCheck(), *sixteen_check.CHECK_FUNCTIONS[1:]],
        )

        assert sixteen_check.main(["--strict"]) == 1

        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "forced audit failure" in captured.out

    def test_non_strict_exit_zero_even_when_a_check_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        self._clear_runtime_audit_env(monkeypatch)
        monkeypatch.setattr(
            sixteen_check,
            "CHECK_FUNCTIONS",
            [self._FailingCheck(), *sixteen_check.CHECK_FUNCTIONS[1:]],
        )

        assert sixteen_check.main([]) == 0

        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "forced audit failure" in captured.out

    def test_strict_json_exit_zero_and_emits_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        import json

        self._clear_runtime_audit_env(monkeypatch)

        assert sixteen_check.main(["--strict", "--json"]) == 0

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["all_passed"] is True
        assert payload["strict"] is True
        assert len(payload["checks"]) == EXPECTED_AUDIT_CHECK_COUNT
        assert all(check["passed"] for check in payload["checks"])
