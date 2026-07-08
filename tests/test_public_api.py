"""Preserve public API.

Assert that the Pitwall RunPod Pod client preserves the expected public API
surface so lease code can call the client without adapter glue.

Every public function, class, and exception in the contract above
 must have a stable counterpart in
``pitwall.runpod_client.pods`` with the same name and signature.
"""

from __future__ import annotations

import inspect

import pytest

import pitwall.runpod_client.pods as pods

EXPECTED_ERROR_CLASSES = (
    "RunPodError",
    "NoCapacityError",
    "ProviderAttachHangRecoveryRequested",
    "PodStartupTimeout",
    "PodVolumeAttachTimeout",
    "PodStartupFailed",
    "RunPodRestError",
)

EXPECTED_SYNC_FUNCTIONS = (
    "create_pod_with_fallback_sync",
    "wait_for_pod_runtime_sync",
    "get_pods_sync",
    "get_pod_sync",
    "terminate_pod_sync",
)

EXPECTED_ASYNC_FUNCTIONS = (
    "create_pod_with_fallback",
    "wait_for_pod_runtime",
    "get_pods",
    "get_pod",
    "terminate_pod",
    "terminate_all_with_tag",
)

EXPECTED_ALL_EXPORTS = [
    "RunPodError",
    "NoCapacityError",
    "ProviderAttachHangRecoveryRequested",
    "PodStartupFailed",
    "PodStartupTimeout",
    "PodVolumeAttachTimeout",
    "RunPodRestError",
    "UpdatePodRequest",
    "UpdatePodResponse",
    "create_pod_with_fallback",
    "create_pod_with_fallback_sync",
    "get_pods",
    "get_pods_sync",
    "get_pod",
    "get_pod_sync",
    "wait_for_pod_runtime",
    "wait_for_pod_runtime_sync",
    "terminate_pod",
    "terminate_pod_sync",
    "terminate_all_with_tag",
    "start_pod",
    "stop_pod",
    "reset_pod",
    "restart_pod",
    "update_pod",
]


class TestErrorClassesPreserved:
    """Every expected error class must exist in the Pitwall module."""

    @pytest.mark.parametrize("name", EXPECTED_ERROR_CLASSES)
    def test_error_class_exists(self, name: str) -> None:
        assert hasattr(pods, name), f"missing error class: {name}"

    @pytest.mark.parametrize("name", EXPECTED_ERROR_CLASSES)
    def test_error_class_is_exception_subclass(self, name: str) -> None:
        cls = getattr(pods, name)
        assert issubclass(cls, BaseException)

    def test_runpod_error_is_runtime_error(self) -> None:
        assert issubclass(pods.RunPodError, RuntimeError)

    def test_no_capacity_is_runpod_error(self) -> None:
        assert issubclass(pods.NoCapacityError, pods.RunPodError)

    def test_pod_startup_timeout_is_runpod_error(self) -> None:
        assert issubclass(pods.PodStartupTimeout, pods.RunPodError)

    def test_pod_startup_failed_is_runpod_error(self) -> None:
        assert issubclass(pods.PodStartupFailed, pods.RunPodError)

    def test_runpod_rest_error_is_runpod_error(self) -> None:
        assert issubclass(pods.RunPodRestError, pods.RunPodError)

    def test_runpod_rest_error_has_attributes(self) -> None:
        exc = pods.RunPodRestError("GET", "pods", 500, "boom")
        assert exc.method == "GET"
        assert exc.path == "pods"
        assert exc.status_code == 500
        assert exc.body == "boom"


class TestSyncFunctionSignaturesPreserved:
    """Sync function signatures must match the expected lease-code contract."""

    @pytest.mark.parametrize("name", EXPECTED_SYNC_FUNCTIONS)
    def test_sync_function_exists(self, name: str) -> None:
        assert hasattr(pods, name), f"missing sync function: {name}"
        assert callable(getattr(pods, name))

    def test_create_pod_with_fallback_sync_signature(self) -> None:
        sig = inspect.signature(pods.create_pod_with_fallback_sync)
        params = list(sig.parameters.keys())
        assert params == [
            "name",
            "template_id",
            "image_name",
            "workload",
            "env",
            "cloud_type_override",
            "network_volume_id",
            "data_center_id",
            "docker_entrypoint",
            "docker_start_cmd",
            "container_registry_auth_id",
            "support_public_ip",
            "max_cost_per_hr",
            "max_pod_attempts",
            "timeout_per_attempt_s",
            "startup_timeout_s",
            "startup_poll_s",
            "volume_attach_timeout_s",
            "pre_readiness_callback",
            "wait_for_readiness",
        ]
        assert sig.parameters["name"].kind == inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["support_public_ip"].default is False
        assert sig.parameters["timeout_per_attempt_s"].default == 120.0
        assert sig.parameters["startup_timeout_s"].default == 600.0
        assert sig.parameters["startup_poll_s"].default == 15.0
        assert sig.parameters["volume_attach_timeout_s"].default is None

    def test_wait_for_pod_runtime_sync_signature(self) -> None:
        sig = inspect.signature(pods.wait_for_pod_runtime_sync)
        params = list(sig.parameters.keys())
        assert params == ["pod_id", "initial", "timeout_s", "poll_s", "volume_attach_timeout_s"]
        assert sig.parameters["pod_id"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert sig.parameters["initial"].default is None
        assert sig.parameters["timeout_s"].default == 600.0
        assert sig.parameters["poll_s"].default == 15.0
        assert sig.parameters["volume_attach_timeout_s"].default is None

    def test_get_pods_sync_signature(self) -> None:
        sig = inspect.signature(pods.get_pods_sync)
        assert list(sig.parameters.keys()) == []

    def test_get_pod_sync_signature(self) -> None:
        sig = inspect.signature(pods.get_pod_sync)
        assert list(sig.parameters.keys()) == ["pod_id"]

    def test_terminate_pod_sync_signature(self) -> None:
        sig = inspect.signature(pods.terminate_pod_sync)
        assert list(sig.parameters.keys()) == ["pod_id"]


class TestAsyncFunctionSignaturesPreserved:
    """Async function signatures must match the expected lease-code contract."""

    @pytest.mark.parametrize("name", EXPECTED_ASYNC_FUNCTIONS)
    def test_async_function_exists(self, name: str) -> None:
        assert hasattr(pods, name), f"missing async function: {name}"
        fn = getattr(pods, name)
        assert inspect.iscoroutinefunction(fn), f"{name} must be async"

    def test_create_pod_with_fallback_signature(self) -> None:
        sig = inspect.signature(pods.create_pod_with_fallback)
        params = list(sig.parameters.keys())
        assert params == [
            "name",
            "template_id",
            "image_name",
            "workload",
            "env",
            "cloud_type_override",
            "network_volume_id",
            "data_center_id",
            "docker_entrypoint",
            "docker_start_cmd",
            "container_registry_auth_id",
            "support_public_ip",
            "max_cost_per_hr",
            "max_pod_attempts",
            "timeout_per_attempt_s",
            "startup_timeout_s",
            "startup_poll_s",
            "volume_attach_timeout_s",
            "pre_readiness_callback",
            "wait_for_readiness",
        ]
        assert sig.parameters["name"].kind == inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["volume_attach_timeout_s"].default is None

    def test_wait_for_pod_runtime_signature(self) -> None:
        sig = inspect.signature(pods.wait_for_pod_runtime)
        params = list(sig.parameters.keys())
        assert params == ["pod_id", "initial", "timeout_s", "poll_s", "volume_attach_timeout_s"]
        assert sig.parameters["pod_id"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert sig.parameters["timeout_s"].default == 600.0
        assert sig.parameters["poll_s"].default == 15.0
        assert sig.parameters["volume_attach_timeout_s"].default is None

    def test_get_pods_signature(self) -> None:
        sig = inspect.signature(pods.get_pods)
        assert list(sig.parameters.keys()) == []

    def test_get_pod_signature(self) -> None:
        sig = inspect.signature(pods.get_pod)
        assert list(sig.parameters.keys()) == ["pod_id"]

    def test_terminate_pod_signature(self) -> None:
        sig = inspect.signature(pods.terminate_pod)
        assert list(sig.parameters.keys()) == ["pod_id"]

    def test_terminate_all_with_tag_signature(self) -> None:
        sig = inspect.signature(pods.terminate_all_with_tag)
        params = list(sig.parameters.keys())
        assert params == ["name_prefix"]
        assert sig.parameters["name_prefix"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD


class TestAllExportsComplete:
    """The __all__ list must contain every expected public name."""

    def test_all_exports_present(self) -> None:
        for name in EXPECTED_ALL_EXPORTS:
            assert name in pods.__all__, f"{name!r} missing from pods.__all__"

    def test_all_exports_importable(self) -> None:
        for name in pods.__all__:
            assert hasattr(pods, name), f"__all__ lists {name!r} but it is not importable"

    def test_all_exports_match_expected_surface(self) -> None:
        all_set = set(pods.__all__)
        assert all_set == set(EXPECTED_ALL_EXPORTS)


class TestSyncAsyncPairsMatch:
    """Each sync function must have a corresponding async wrapper with the same parameters."""

    @pytest.mark.parametrize(
        "sync_name,async_name",
        [
            ("create_pod_with_fallback_sync", "create_pod_with_fallback"),
            ("wait_for_pod_runtime_sync", "wait_for_pod_runtime"),
            ("get_pods_sync", "get_pods"),
            ("get_pod_sync", "get_pod"),
            ("terminate_pod_sync", "terminate_pod"),
        ],
    )
    def test_sync_async_parameter_names_match(self, sync_name: str, async_name: str) -> None:
        sync_sig = inspect.signature(getattr(pods, sync_name))
        async_sig = inspect.signature(getattr(pods, async_name))
        assert list(sync_sig.parameters.keys()) == list(async_sig.parameters.keys()), (
            f"{sync_name} params {list(sync_sig.parameters.keys())} != "
            f"{async_name} params {list(async_sig.parameters.keys())}"
        )

    @pytest.mark.parametrize(
        "sync_name,async_name",
        [
            ("create_pod_with_fallback_sync", "create_pod_with_fallback"),
            ("wait_for_pod_runtime_sync", "wait_for_pod_runtime"),
            ("get_pods_sync", "get_pods"),
            ("get_pod_sync", "get_pod"),
            ("terminate_pod_sync", "terminate_pod"),
        ],
    )
    def test_sync_async_defaults_match(self, sync_name: str, async_name: str) -> None:
        sync_sig = inspect.signature(getattr(pods, sync_name))
        async_sig = inspect.signature(getattr(pods, async_name))
        for pname in sync_sig.parameters:
            sync_default = sync_sig.parameters[pname].default
            async_default = async_sig.parameters[pname].default
            assert sync_default == async_default, (
                f"{sync_name}.{pname} default {sync_default!r} != "
                f"{async_name}.{pname} default {async_default!r}"
            )


class TestPackageReExports:
    """The package __init__ must re-export every pods.py public name."""

    import pitwall.runpod_client as pkg

    @pytest.mark.parametrize("name", EXPECTED_ALL_EXPORTS)
    def test_re_exported_from_package(self, name: str) -> None:
        assert hasattr(self.pkg, name), (
            f"{name!r} not re-exported from pitwall.runpod_client.__init__"
        )

    @pytest.mark.parametrize("name", EXPECTED_ALL_EXPORTS)
    def test_re_export_is_same_object(self, name: str) -> None:
        pkg_obj = getattr(self.pkg, name)
        pods_obj = getattr(pods, name)
        assert pkg_obj is pods_obj, f"{name!r} re-export is not the same object as pods.{name!r}"
