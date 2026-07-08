"""RunPod GPU name validation.

RunPod pod creation expects the exact ``gpuTypeId`` display names returned by
RunPod. Short aliases such as ``H100``, ``L4``, and ``RTX4090`` are rejected
instead of normalized so callers do not silently launch against the wrong
capacity lane.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

_CANONICAL_GPU_NAMES = (
    "NVIDIA H100 80GB HBM3",
    "NVIDIA H100 NVL",
    "NVIDIA H200",
    "NVIDIA H200 NVL",
    "NVIDIA B200",
    "NVIDIA A100 80GB",
    "NVIDIA A100 80GB PCIe",
    "NVIDIA A100 40GB",
    "NVIDIA A6000",
    "NVIDIA RTX A6000",
    "NVIDIA A40",
    "NVIDIA L40",
    "NVIDIA L40S",
    "NVIDIA L4",
    "NVIDIA RTX 6000 Ada",
    "NVIDIA RTX 4090",
    "NVIDIA GeForce RTX 4090",
    "NVIDIA RTX A5000",
    "NVIDIA RTX A4500",
    "NVIDIA RTX A4000",
    "NVIDIA RTX 5000 Ada Generation",
    "NVIDIA RTX 4000 Ada Generation",
)

CANONICAL_GPU_NAMES = frozenset(_CANONICAL_GPU_NAMES)
CANONICAL_RUNPOD_GPU_NAMES = CANONICAL_GPU_NAMES
CANONICAL_GPU_NAME_MAP: Mapping[str, str] = MappingProxyType(
    {gpu_name: gpu_name for gpu_name in _CANONICAL_GPU_NAMES}
)


def _lookup_key(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


_CANONICAL_GPU_NAME_BY_KEY: Mapping[str, str] = MappingProxyType(
    {_lookup_key(gpu_name): gpu_name for gpu_name in _CANONICAL_GPU_NAMES}
)
_SHORTHAND_GPU_SUGGESTIONS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "H100": ("NVIDIA H100 80GB HBM3", "NVIDIA H100 NVL"),
        "H200": ("NVIDIA H200", "NVIDIA H200 NVL"),
        "B200": ("NVIDIA B200",),
        "A100": ("NVIDIA A100 80GB", "NVIDIA A100 80GB PCIe", "NVIDIA A100 40GB"),
        "A6000": ("NVIDIA A6000", "NVIDIA RTX A6000"),
        "A40": ("NVIDIA A40",),
        "L40": ("NVIDIA L40", "NVIDIA L40S"),
        "L40S": ("NVIDIA L40S",),
        "L4": ("NVIDIA L4",),
        "RTX6000ADA": ("NVIDIA RTX 6000 Ada",),
        "RTX4090": ("NVIDIA GeForce RTX 4090", "NVIDIA RTX 4090"),
        "4090": ("NVIDIA GeForce RTX 4090", "NVIDIA RTX 4090"),
        "RTXA5000": ("NVIDIA RTX A5000",),
        "RTXA4500": ("NVIDIA RTX A4500",),
        "RTXA4000": ("NVIDIA RTX A4000",),
        "RTX5000ADA": ("NVIDIA RTX 5000 Ada Generation",),
        "RTX4000ADA": ("NVIDIA RTX 4000 Ada Generation",),
    }
)


class NonCanonicalGPUNameError(ValueError):
    """Raised when a RunPod GPU name is not an exact canonical name."""

    def __init__(self, gpu_name: str, suggestions: Iterable[str] = ()) -> None:
        self.gpu_name = gpu_name
        self.suggestions = tuple(suggestions)
        message = (
            f"RunPod GPU name {gpu_name!r} is not canonical; use the exact "
            "RunPod gpuTypeId full name"
        )
        if self.suggestions:
            message = f"{message}. Suggested canonical name(s): {', '.join(self.suggestions)}"
        else:
            message = f"{message}. Shorthand GPU names are rejected."
        super().__init__(message)


def is_canonical_gpu_name(gpu_name: str) -> bool:
    """Return true only for exact RunPod canonical GPU names."""

    return gpu_name in CANONICAL_GPU_NAMES


def non_canonical_gpu_names(gpu_names: Iterable[str]) -> list[str]:
    """Return the names that are not exact RunPod canonical GPU names."""

    return [gpu_name for gpu_name in gpu_names if not is_canonical_gpu_name(gpu_name)]


def canonical_gpu_name_suggestions(gpu_name: str) -> tuple[str, ...]:
    """Return diagnostic suggestions without accepting aliases as input."""

    key = _lookup_key(gpu_name)
    if exact_case_suggestion := _CANONICAL_GPU_NAME_BY_KEY.get(key):
        return (exact_case_suggestion,)
    return _SHORTHAND_GPU_SUGGESTIONS.get(key, ())


def validate_canonical_gpu_name(gpu_name: str) -> str:
    """Validate and return one exact canonical RunPod GPU name."""

    if is_canonical_gpu_name(gpu_name):
        return gpu_name
    raise NonCanonicalGPUNameError(
        gpu_name,
        canonical_gpu_name_suggestions(gpu_name),
    )


def validate_canonical_gpu_names(gpu_names: Iterable[str]) -> list[str]:
    """Validate and return exact canonical RunPod GPU names in input order."""

    return [validate_canonical_gpu_name(gpu_name) for gpu_name in gpu_names]


validate_gpu_type = validate_canonical_gpu_name
validate_gpu_types = validate_canonical_gpu_names


__all__ = [
    "CANONICAL_GPU_NAME_MAP",
    "CANONICAL_GPU_NAMES",
    "CANONICAL_RUNPOD_GPU_NAMES",
    "NonCanonicalGPUNameError",
    "canonical_gpu_name_suggestions",
    "is_canonical_gpu_name",
    "non_canonical_gpu_names",
    "validate_canonical_gpu_name",
    "validate_canonical_gpu_names",
    "validate_gpu_type",
    "validate_gpu_types",
]
