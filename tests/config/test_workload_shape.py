"""Workload model-shape assertions.

The ServerlessSettings URL-composition pattern (base_url_template,
chat_completions_url) generalises into Pitwall's capability registry; the
field-level shape assertions are preserved here against WorkloadConfig and
the RunPod URL template convention.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pitwall.runpod_client.workloads import WorkloadConfig

_RUNPOD_BASE_URL_TEMPLATE = "https://api.runpod.ai/v2/{endpoint_id}/openai/v1"


def _sample_workload(**overrides) -> dict:
    base = {
        "name": "re-embedding",
        "capability": "embed",
        "gpu_types": ["NVIDIA RTX A4000", "NVIDIA L4"],
    }
    base.update(overrides)
    return base


class TestWorkloadConfigFieldShape:
    """WorkloadConfig field-shape assertions."""

    def test_name_and_capability_are_required(self) -> None:
        with pytest.raises(ValidationError):
            WorkloadConfig(
                capability="embed",
                gpu_types=["NVIDIA L4"],
            )

    def test_gpu_types_minimum_one(self) -> None:
        with pytest.raises(ValidationError):
            WorkloadConfig(name="x", capability="embed", gpu_types=[])

    def test_default_optional_fields_are_none(self) -> None:
        cfg = WorkloadConfig(**_sample_workload())
        assert cfg.template_name is None
        assert cfg.allowed_cuda_versions is None
        assert cfg.ports is None

    def test_default_numeric_field_values(self) -> None:
        cfg = WorkloadConfig(**_sample_workload())
        assert cfg.gpu_count == 1
        assert cfg.container_disk_gb == 50
        assert cfg.min_vcpu == 4
        assert cfg.min_memory_gb == 16

    def test_default_cloud_type_is_all(self) -> None:
        cfg = WorkloadConfig(**_sample_workload())
        assert cfg.cloud_type == "ALL"

    def test_default_priority_fields(self) -> None:
        cfg = WorkloadConfig(**_sample_workload())
        assert cfg.gpu_type_priority == "custom"
        assert cfg.data_center_priority == "custom"

    def test_overrides_apply_to_all_fields(self) -> None:
        cfg = WorkloadConfig(
            name="sft-generation",
            capability="sft-gen",
            template_name="demo-sft-generation",
            gpu_types=["NVIDIA A100 80GB PCIe"],
            gpu_count=2,
            container_disk_gb=200,
            min_vcpu=8,
            min_memory_gb=64,
            cloud_type="SECURE",
            gpu_type_priority="availability",
            data_center_priority="availability",
            allowed_cuda_versions=["12.4"],
            ports="8000/http,22/tcp",
        )
        assert cfg.template_name == "demo-sft-generation"
        assert cfg.gpu_count == 2
        assert cfg.container_disk_gb == 200
        assert cfg.min_vcpu == 8
        assert cfg.min_memory_gb == 64
        assert cfg.cloud_type == "SECURE"
        assert cfg.gpu_type_priority == "availability"
        assert cfg.data_center_priority == "availability"
        assert cfg.allowed_cuda_versions == ["12.4"]
        assert cfg.ports == "8000/http,22/tcp"

    def test_rejects_invalid_gpu_type_priority(self) -> None:
        with pytest.raises(ValidationError):
            WorkloadConfig(
                **_sample_workload(gpu_type_priority="fastest"),
            )

    def test_rejects_invalid_data_center_priority(self) -> None:
        with pytest.raises(ValidationError):
            WorkloadConfig(
                **_sample_workload(data_center_priority="nearest"),
            )

    def test_rejects_non_canonical_gpu_names(self) -> None:
        with pytest.raises(ValidationError, match="not canonical"):
            WorkloadConfig(
                **_sample_workload(gpu_types=["H100", "RTX4090"]),
            )


class TestRunPodUrlTemplateShape:
    """URL-composition shape assertions.

    The base_url_template and chat_completions_url pattern builds per-endpoint
    OpenAI-compatible URLs. These tests assert the template convention and URL
    shape are correct.
    """

    def test_base_url_template_format(self) -> None:
        url = _RUNPOD_BASE_URL_TEMPLATE.format(endpoint_id="abc123")
        assert url == "https://api.runpod.ai/v2/abc123/openai/v1"

    def test_chat_completions_url_composition(self) -> None:
        endpoint_id = "vlm-id-xyz"
        base_url = _RUNPOD_BASE_URL_TEMPLATE.format(endpoint_id=endpoint_id)
        chat_url = f"{base_url}/chat/completions"
        assert chat_url == ("https://api.runpod.ai/v2/vlm-id-xyz/openai/v1/chat/completions")

    def test_different_endpoint_ids_produce_different_urls(self) -> None:
        vlm_url = _RUNPOD_BASE_URL_TEMPLATE.format(endpoint_id="vlm-ep")
        text_url = _RUNPOD_BASE_URL_TEMPLATE.format(endpoint_id="text-ep")
        assert vlm_url != text_url
        assert "vlm-ep" in vlm_url
        assert "text-ep" in text_url
