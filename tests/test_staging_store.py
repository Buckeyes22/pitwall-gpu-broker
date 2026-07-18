from __future__ import annotations

import builtins
import importlib
import sys
from typing import Any

import pytest

from pitwall.staging_store import (
    CloudflareR2StagingStore,
    NoOpStagingStore,
    get_staging_store,
)


def test_default_staging_store_noops_without_r2_config() -> None:
    store = get_staging_store(environ={})

    assert isinstance(store, NoOpStagingStore)
    assert store.vend_pod_credentials() == {}
    assert store.cleanup_pod_artifacts([{"id": "pod-1", "name": "pod"}]) == []


def test_complete_r2_temp_credential_config_selects_cloudflare_store() -> None:
    store = get_staging_store(
        environ={
            "R2_ENDPOINT": "https://r2.example.test",
            "R2_BUCKET_STAGING": "pitwall-staging",
            "CLOUDFLARE_ACCOUNT_ID": "account",
            "CLOUDFLARE_API_TOKEN": "token",
            "R2_PARENT_ACCESS_KEY_ID": "parent-access",
        }
    )

    assert isinstance(store, CloudflareR2StagingStore)


def test_complete_r2_cleanup_config_selects_cloudflare_store() -> None:
    store = get_staging_store(
        environ={
            "R2_ENDPOINT": "https://r2.example.test",
            "R2_BUCKET_STAGING": "pitwall-staging",
            "R2_ACCESS_KEY": "cleanup-access",
            "R2_SECRET_KEY": "cleanup-secret",
        }
    )

    assert isinstance(store, CloudflareR2StagingStore)


def test_cloudflare_store_vends_through_temp_credential_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pitwall import r2_temp_credentials

    monkeypatch.setattr(
        r2_temp_credentials,
        "vend_r2_temp_credential_pod_env",
        lambda: {"AWS_ACCESS_KEY_ID": "tmp-access"},
    )

    assert CloudflareR2StagingStore().vend_pod_credentials() == {"AWS_ACCESS_KEY_ID": "tmp-access"}


def test_cloudflare_store_cleans_up_through_cleanup_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pitwall import r2_staging_cleanup

    calls: list[dict[str, Any]] = []

    def fake_cleanup_staging_for_pods(
        pods: list[dict[str, Any]],
        **kwargs: str,
    ) -> list[str]:
        calls.append({"pods": pods, **kwargs})
        return ["deleted"]

    monkeypatch.setattr(
        r2_staging_cleanup,
        "cleanup_staging_for_pods",
        fake_cleanup_staging_for_pods,
    )

    store = CloudflareR2StagingStore(
        environ={
            "R2_ENDPOINT": "https://r2.example.test",
            "R2_BUCKET_STAGING": "pitwall-staging",
            "R2_ACCESS_KEY": "cleanup-access",
            "R2_SECRET_KEY": "cleanup-secret",
            "PITWALL_R2_TEMP_CREDENTIAL_PREFIXES": "debug-logs",
        }
    )

    assert store.cleanup_pod_artifacts([{"id": "pod-1", "name": "pod"}]) == ["deleted"]
    assert calls == [
        {
            "pods": [{"id": "pod-1", "name": "pod"}],
            "endpoint": "https://r2.example.test",
            "access_key": "cleanup-access",
            "secret_key": "cleanup-secret",
            "bucket": "pitwall-staging",
            "prefix": "debug-logs/",
        }
    ]


def test_r2_staging_cleanup_import_does_not_import_boto3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module_name in (
        "pitwall.api.admin.r2_staging_cleanup",
        "pitwall.r2_staging_cleanup",
        "boto3",
        "botocore",
        "botocore.config",
    ):
        sys.modules.pop(module_name, None)

    real_import = builtins.__import__

    def blocked_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "boto3" or name.startswith("botocore"):
            raise ModuleNotFoundError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    module = importlib.import_module("pitwall.api.admin.r2_staging_cleanup")

    assert module.DEFAULT_R2_DEBUG_LOG_PREFIX == "debug-logs/"
