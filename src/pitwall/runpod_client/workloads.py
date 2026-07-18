"""Declarative workload config for RunPod pod provisioning."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from pitwall.runpod_client.gpu import validate_canonical_gpu_names


class WorkloadConfig(BaseModel):
    """Everything needed to launch a RunPod pod for one workload."""

    name: str
    capability: str
    template_name: str | None = None
    gpu_types: list[str] = Field(min_length=1)
    gpu_count: int = 1
    container_disk_gb: int = 50
    min_vcpu: int = 4
    min_memory_gb: int = 16
    cloud_type: str = "ALL"
    gpu_type_priority: Literal["custom", "availability"] = "custom"
    data_center_priority: Literal["custom", "availability"] = "custom"
    allowed_cuda_versions: list[str] | None = None
    ports: str | None = None

    @field_validator("gpu_types")
    @classmethod
    def _validate_gpu_types(cls, gpu_types: list[str]) -> list[str]:
        return validate_canonical_gpu_names(gpu_types)


__all__ = ["WorkloadConfig"]
