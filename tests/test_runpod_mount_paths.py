from __future__ import annotations

import pytest

from pitwall.core import ProviderType
from pitwall.runpod_client import (
    POD_PROVIDER_TYPES,
    POD_VOLUME_MOUNT_PATH,
    PROVIDER_TYPE_VOLUME_MOUNT_PATHS,
    SERVERLESS_PROVIDER_TYPES,
    SERVERLESS_VOLUME_MOUNT_PATH,
    WorkloadConfig,
    provider_type_volume_mount_path,
)
from pitwall.runpod_client import (
    ProviderType as RunPodClientProviderType,
)


def test_l10_mount_path_constants_keep_pods_and_serverless_distinct() -> None:
    assert RunPodClientProviderType is ProviderType
    assert POD_VOLUME_MOUNT_PATH == "/workspace"
    assert SERVERLESS_VOLUME_MOUNT_PATH == "/runpod-volume"
    assert POD_VOLUME_MOUNT_PATH != SERVERLESS_VOLUME_MOUNT_PATH


def test_provider_type_mount_paths_cover_all_runpod_provider_types() -> None:
    assert set(PROVIDER_TYPE_VOLUME_MOUNT_PATHS) == set(ProviderType)
    assert frozenset({ProviderType.POD_LEASE}) == POD_PROVIDER_TYPES
    assert (
        frozenset(
            {
                ProviderType.SERVERLESS_QUEUE,
                ProviderType.SERVERLESS_LB,
                ProviderType.PUBLIC_ENDPOINT,
            }
        )
        == SERVERLESS_PROVIDER_TYPES
    )

    assert PROVIDER_TYPE_VOLUME_MOUNT_PATHS[ProviderType.POD_LEASE] == "/workspace"
    for provider_type in SERVERLESS_PROVIDER_TYPES:
        assert PROVIDER_TYPE_VOLUME_MOUNT_PATHS[provider_type] == "/runpod-volume"


def test_provider_type_mount_path_helper_accepts_enum_and_string_values() -> None:
    assert provider_type_volume_mount_path(ProviderType.POD_LEASE) == "/workspace"
    assert provider_type_volume_mount_path("serverless_queue") == "/runpod-volume"
    assert provider_type_volume_mount_path("serverless_lb") == "/runpod-volume"
    assert provider_type_volume_mount_path("public_endpoint") == "/runpod-volume"


def test_provider_type_mount_path_helper_rejects_unknown_provider_type() -> None:
    with pytest.raises(ValueError, match="unknown provider_type 'local_gpu'"):
        provider_type_volume_mount_path("local_gpu")


def test_workload_config_does_not_expose_mount_paths_to_consumers() -> None:
    assert "mount_path" not in WorkloadConfig.model_fields
    assert "volume_mount" not in WorkloadConfig.model_fields
    assert "volume_mount_path" not in WorkloadConfig.model_fields
