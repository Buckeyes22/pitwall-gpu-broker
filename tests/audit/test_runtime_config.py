"""Tests for RuntimeAuditConfig.

Add audit tests for the RuntimeAuditConfig class.
"""

from __future__ import annotations

import pytest


class TestRuntimeAuditConfigDefaults:
    """Happy-path tests: RuntimeAuditConfig returns correct defaults."""

    def test_gpu_ids_returns_default_canonical_names(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        gpus = cfg.gpu_ids()
        assert isinstance(gpus, list)
        assert len(gpus) == 3
        assert "NVIDIA H100 80GB HBM3" in gpus
        assert "NVIDIA L4" in gpus
        assert "NVIDIA A100 80GB" in gpus

    def test_workloads_returns_three_workloads(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        workloads = cfg.workloads()
        assert len(workloads) == 3
        names = {w["name"] for w in workloads}
        assert names == {"vllm", "embed", "slim"}

    def test_workloads_have_disk_sizes(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        workloads = cfg.workloads()
        for wl in workloads:
            assert wl["disk_gb"] >= 20

    def test_launch_params_defaults_to_secure_cloud(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        params = cfg.launch_params()
        assert params["cloud_type"] == "SECURE"
        assert params["networkVolumeId"] == ""

    def test_readiness_config_returns_runtime_probe_field(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        rc = cfg.readiness_config()
        assert rc["probe_field"] == "runtime"

    def test_cost_config_has_correct_order(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        cc = cfg.cost_config()
        assert cc["check_order"] == ["cost", "readiness"]

    def test_timeout_config_has_reasonable_defaults(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        tc = cfg.timeout_config()
        assert tc["executionTimeout"] == 3600
        assert tc["executionTimeoutMax"] == 7200
        assert tc["ttl"] == 7200
        assert tc["expected_queue_time"] == 300

    def test_webhook_config_is_idempotent_and_fast_200(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        wc = cfg.webhook_config()
        assert wc["idempotent"] is True
        assert wc["fast_200"] is True

    def test_retention_config_has_sync_and_async_windows(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        rc = cfg.retention_config()
        assert rc["sync_retention_s"] == 60
        assert rc["async_retention_s"] == 1800
        assert rc["persist_before_expiry"] is True
        assert rc["sync_persist_deadline_s"] == 30
        assert rc["async_persist_deadline_s"] == 300

    def test_volume_config_defaults_to_empty(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        vc = cfg.volume_config()
        assert vc["networkVolumeId"] == ""
        assert vc["dataCenterIds"] == []

    def test_probe_config_ssh_first(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        pc = cfg.probe_config()
        assert pc["ssh_first"] is True
        assert pc["primary_probe"] == "ssh_localhost"
        assert "ssh_localhost" in pc["probe_methods"]
        assert "runpod_proxy" in pc["probe_methods"]

    def test_image_config_has_reasonable_timeouts(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        ic = cfg.image_config()
        assert ic["image_pull_timeout_s"] == 600
        assert ic["startup_timeout_s"] == 600

    def test_disk_config_has_all_workloads(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        dc = cfg.disk_config()
        assert dc["per_workload"]["vllm"] == 80
        assert dc["per_workload"]["embed"] == 40
        assert dc["per_workload"]["slim"] == 20

    def test_template_config_cache_enabled(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        tc = cfg.template_config()
        assert tc["cache_enabled"] is True
        assert tc["create_on_cache_miss"] is True
        assert tc["reuse_on_cache_hit"] is True

    def test_registry_config_has_all_prefixes(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig
        from pitwall.runpod_client.registry import (
            DOCKER_HUB_PREFIX,
            GHCR_PREFIX,
            GITLAB_REGISTRY_PREFIX,
        )

        cfg = RuntimeAuditConfig()
        rc = cfg.registry_config()
        mapping = rc["prefix_to_auth_id"]
        assert GHCR_PREFIX in mapping
        assert GITLAB_REGISTRY_PREFIX in mapping
        assert DOCKER_HUB_PREFIX in mapping

    def test_provider_fixtures_include_hibernated_vllm(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        fixtures = cfg.provider_fixtures()
        assert fixtures
        vllm_provider = fixtures[0]
        assert vllm_provider["provider_type"] == "serverless_lb"
        assert vllm_provider["config"]["container_disk_gb"] == 80
        assert vllm_provider["config"]["workers"]["workers_min"] == 0
        assert "hf download" in vllm_provider["config"]["worker_image"]["download_command"]

    def test_terminate_config_treats_404_as_success(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        tc = cfg.terminate_config()
        assert tc["treat_404_as_success"] is True

    def test_kill_switch_config_has_atomic_3_step(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        kc = cfg.kill_switch_config()
        assert kc["atomic"] is True
        assert kc["steps"] == ["list_pods", "terminate_all", "verify"]
        assert kc["budget_s"] == 25

    def test_get_returns_none_when_key_missing(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        result = cfg.get("NONEXISTENT_KEY")
        assert result is None

    def test_get_returns_default_when_key_missing(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        result = cfg.get("NONEXISTENT_KEY", "default_value")
        assert result == "default_value"


class TestRuntimeAuditConfigEnvOverrides:
    """Deliberate-failure / edge-case tests: env var overrides work correctly."""

    def test_gpu_ids_from_env_single(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        monkeypatch.setenv("PITWALL_AUDIT_GPU_IDS", "NVIDIA H100 80GB HBM3")
        cfg = RuntimeAuditConfig()
        gpus = cfg.gpu_ids()
        assert gpus == ["NVIDIA H100 80GB HBM3"]

    def test_gpu_ids_from_env_multiple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        monkeypatch.setenv("PITWALL_AUDIT_GPU_IDS", "NVIDIA H100 80GB HBM3, NVIDIA L4")
        cfg = RuntimeAuditConfig()
        gpus = cfg.gpu_ids()
        assert len(gpus) == 2
        assert "NVIDIA H100 80GB HBM3" in gpus
        assert "NVIDIA L4" in gpus

    def test_gpu_ids_from_env_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        monkeypatch.setenv("PITWALL_AUDIT_GPU_IDS", " NVIDIA H100 80GB HBM3 , NVIDIA L4 ")
        cfg = RuntimeAuditConfig()
        gpus = cfg.gpu_ids()
        assert "NVIDIA H100 80GB HBM3" in gpus
        assert "NVIDIA L4" in gpus

    def test_gpu_ids_from_env_ignores_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        monkeypatch.setenv("PITWALL_AUDIT_GPU_IDS", "NVIDIA H100 80GB HBM3,,NVIDIA L4")
        cfg = RuntimeAuditConfig()
        gpus = cfg.gpu_ids()
        assert len(gpus) == 2

    def test_cloud_type_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        monkeypatch.setenv("PITWALL_AUDIT_CLOUD_TYPE", "ALL")
        cfg = RuntimeAuditConfig()
        params = cfg.launch_params()
        assert params["cloud_type"] == "ALL"

    def test_network_volume_id_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        monkeypatch.setenv("RUNPOD_NETWORK_VOLUME_ID", "vol-abc123")
        cfg = RuntimeAuditConfig()
        params = cfg.launch_params()
        assert params["networkVolumeId"] == "vol-abc123"

    def test_volume_config_with_dc_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        monkeypatch.setenv("RUNPOD_NETWORK_VOLUME_ID", "vol-abc123")
        monkeypatch.setenv("RUNPOD_DATA_CENTER_ID", "US-KS-2")
        cfg = RuntimeAuditConfig()
        vc = cfg.volume_config()
        assert vc["networkVolumeId"] == "vol-abc123"
        assert vc["dataCenterIds"] == ["US-KS-2"]

    def test_volume_config_empty_dc_id_gives_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        monkeypatch.setenv("RUNPOD_NETWORK_VOLUME_ID", "vol-abc123")
        monkeypatch.setenv("RUNPOD_DATA_CENTER_ID", "")
        cfg = RuntimeAuditConfig()
        vc = cfg.volume_config()
        assert vc["dataCenterIds"] == []

    def test_timeout_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        monkeypatch.setenv("PITWALL_AUDIT_EXEC_TIMEOUT_S", "1800")
        monkeypatch.setenv("PITWALL_AUDIT_EXEC_TIMEOUT_MAX_S", "3600")
        monkeypatch.setenv("PITWALL_DEFAULT_LEASE_TTL_S", "3600")
        monkeypatch.setenv("PITWALL_AUDIT_QUEUE_TIME_S", "600")
        cfg = RuntimeAuditConfig()
        tc = cfg.timeout_config()
        assert tc["executionTimeout"] == 1800
        assert tc["executionTimeoutMax"] == 3600
        assert tc["ttl"] == 3600
        assert tc["expected_queue_time"] == 600

    def test_image_timeout_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        monkeypatch.setenv("PITWALL_IMAGE_PULL_TIMEOUT_S", "900")
        monkeypatch.setenv("PITWALL_AUDIT_STARTUP_TIMEOUT_S", "450")
        cfg = RuntimeAuditConfig()
        ic = cfg.image_config()
        assert ic["image_pull_timeout_s"] == 900
        assert ic["startup_timeout_s"] == 450

    def test_registry_auth_id_ghcr_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig
        from pitwall.runpod_client.registry import GHCR_PREFIX

        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID_GHCR", "env-ghcr-auth")
        cfg = RuntimeAuditConfig()
        rc = cfg.registry_config()
        assert rc["prefix_to_auth_id"][GHCR_PREFIX] == "env-ghcr-auth"

    def test_registry_auth_id_gitlab_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig
        from pitwall.runpod_client.registry import GITLAB_REGISTRY_PREFIX

        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID_GITLAB", "env-gitlab-auth")
        cfg = RuntimeAuditConfig()
        rc = cfg.registry_config()
        assert rc["prefix_to_auth_id"][GITLAB_REGISTRY_PREFIX] == "env-gitlab-auth"

    def test_registry_auth_id_docker_hub_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig
        from pitwall.runpod_client.registry import DOCKER_HUB_PREFIX

        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID_DOCKER_HUB", "env-docker-auth")
        cfg = RuntimeAuditConfig()
        rc = cfg.registry_config()
        assert rc["prefix_to_auth_id"][DOCKER_HUB_PREFIX] == "env-docker-auth"

    def test_registry_auth_id_falls_back_to_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig
        from pitwall.runpod_client.registry import GHCR_PREFIX

        monkeypatch.delenv("RUNPOD_REGISTRY_AUTH_ID_GHCR", raising=False)
        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID", "legacy-auth")
        cfg = RuntimeAuditConfig()
        rc = cfg.registry_config()
        assert rc["prefix_to_auth_id"][GHCR_PREFIX] == "legacy-auth"

    def test_registry_auth_id_ghcr_takes_precedence_over_legacy(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig
        from pitwall.runpod_client.registry import GHCR_PREFIX

        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID_GHCR", "specific-ghcr")
        monkeypatch.setenv("RUNPOD_REGISTRY_AUTH_ID", "legacy-auth")
        cfg = RuntimeAuditConfig()
        rc = cfg.registry_config()
        assert rc["prefix_to_auth_id"][GHCR_PREFIX] == "specific-ghcr"


class TestRuntimeAuditConfigProtocol:
    """Tests that RuntimeAuditConfig properly implements the AuditConfig protocol."""

    def test_all_protocol_methods_exist(self) -> None:
        from pitwall.audit._runtime_config import RuntimeAuditConfig

        cfg = RuntimeAuditConfig()
        protocol_methods = [
            "get",
            "gpu_ids",
            "workloads",
            "launch_params",
            "readiness_config",
            "cost_config",
            "timeout_config",
            "webhook_config",
            "retention_config",
            "volume_config",
            "probe_config",
            "image_config",
            "disk_config",
            "template_config",
            "registry_config",
            "terminate_config",
            "kill_switch_config",
        ]
        for method in protocol_methods:
            assert hasattr(cfg, method), f"Missing method: {method}"
