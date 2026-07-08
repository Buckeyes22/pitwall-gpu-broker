from __future__ import annotations

import pytest
from pydantic import ValidationError

from pitwall.audit.sixteen_check import CANONICAL_GPU_NAMES as AUDIT_CANONICAL_GPU_NAMES
from pitwall.runpod_client import (
    CANONICAL_GPU_NAMES,
    NonCanonicalGPUNameError,
    WorkloadConfig,
    canonical_gpu_name_suggestions,
    validate_canonical_gpu_name,
    validate_canonical_gpu_names,
)


def _workload_config(*, gpu_types: list[str]) -> WorkloadConfig:
    return WorkloadConfig(
        name="vision",
        capability="vision",
        gpu_types=gpu_types,
    )


def test_validator_accepts_exact_runpod_gpu_names() -> None:
    assert validate_canonical_gpu_name("NVIDIA L4") == "NVIDIA L4"
    assert validate_canonical_gpu_names(["NVIDIA H100 80GB HBM3", "NVIDIA GeForce RTX 4090"]) == [
        "NVIDIA H100 80GB HBM3",
        "NVIDIA GeForce RTX 4090",
    ]


@pytest.mark.parametrize("gpu_name", ["H100", "L4", "RTX4090"])
def test_validator_rejects_shorthand_gpu_names(gpu_name: str) -> None:
    with pytest.raises(NonCanonicalGPUNameError, match="not canonical"):
        validate_canonical_gpu_name(gpu_name)


def test_validator_suggests_canonical_name_without_accepting_alias() -> None:
    assert canonical_gpu_name_suggestions("L4") == ("NVIDIA L4",)
    with pytest.raises(NonCanonicalGPUNameError):
        validate_canonical_gpu_name("nvidia l4")


def test_workload_config_rejects_shorthand_gpu_names() -> None:
    with pytest.raises(ValidationError, match="RunPod GPU name 'H100' is not canonical"):
        _workload_config(gpu_types=["H100"])


def test_workload_config_accepts_canonical_gpu_names() -> None:
    config = _workload_config(gpu_types=["NVIDIA L4", "NVIDIA B200"])

    assert config.gpu_types == ["NVIDIA L4", "NVIDIA B200"]


def test_audit_uses_runpod_client_canonical_gpu_set() -> None:
    assert AUDIT_CANONICAL_GPU_NAMES is CANONICAL_GPU_NAMES
