"""Risk-floor tests for destructive R2 staging cleanup."""

from __future__ import annotations

from typing import Any

import pytest

from pitwall import r2_staging_cleanup as cleanup


class _Paginator:
    def __init__(self, pages_by_prefix: dict[str, list[dict[str, Any]]]) -> None:
        self.pages_by_prefix = pages_by_prefix
        self.calls: list[tuple[str, str]] = []

    def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
        self.calls.append((Bucket, Prefix))
        return self.pages_by_prefix.get(Prefix, [])


class _Client:
    def __init__(self, pages_by_prefix: dict[str, list[dict[str, Any]]]) -> None:
        self.paginator = _Paginator(pages_by_prefix)
        self.deleted: list[tuple[str, str]] = []

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_objects_v2"
        return self.paginator

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.deleted.append((Bucket, Key))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"endpoint": " "}, "endpoint"),
        ({"access_key": " "}, "access key"),
        ({"secret_key": " "}, "secret key"),
        ({"bucket": " "}, "bucket"),
        ({"pod_id": " "}, "pod_id"),
    ],
)
def test_delete_rejects_missing_required_values(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
    message: str,
) -> None:
    monkeypatch.setattr(cleanup, "_r2_client", lambda *_args, **_kwargs: pytest.fail("no client"))
    arguments = {
        "pod_id": "pod-1",
        "endpoint": "https://r2.example.test",
        "access_key": "access",
        "secret_key": "secret",
        "bucket": "staging",
    }
    arguments.update(overrides)

    with pytest.raises(cleanup.R2StagingCleanupError, match=message):
        cleanup.delete_pod_staging_prefix(**arguments)


def test_delete_lists_both_compatible_prefixes_and_skips_empty_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(
        {
            "custom/pod-7/": [
                {"Contents": [{"Key": "custom/pod-7/a"}, {"Key": ""}, {}]},
                {"Contents": None},
            ],
            "custom/pod-7.log": [{"Contents": [{"Key": "custom/pod-7.log"}]}],
        }
    )
    monkeypatch.setattr(cleanup, "_r2_client", lambda *_args, **_kwargs: client)

    deleted = cleanup.delete_pod_staging_prefix(
        "pod-7",
        endpoint="https://r2.example.test",
        access_key="access",
        secret_key="secret",
        bucket="staging",
        prefix="custom/",
    )

    assert deleted == 2
    assert client.paginator.calls == [
        ("staging", "custom/pod-7/"),
        ("staging", "custom/pod-7.log"),
    ]
    assert client.deleted == [
        ("staging", "custom/pod-7/a"),
        ("staging", "custom/pod-7.log"),
    ]


def test_cleanup_reports_missing_id_success_and_bounded_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def delete(pod_id: str, **_kwargs: str) -> int:
        if pod_id == "pod-fail":
            raise cleanup.R2StagingCleanupError("storage unavailable")
        return 3

    monkeypatch.setattr(cleanup, "delete_pod_staging_prefix", delete)

    results = cleanup.cleanup_staging_for_pods(
        [
            {"name": "missing"},
            {"id": "pod-ok", "name": "ok"},
            {"id": "pod-fail"},
        ],
        endpoint="https://r2.example.test",
        access_key="access",
        secret_key="secret",
        bucket="staging",
    )

    assert results == [
        cleanup.StagedPodArtifacts("<unknown>", "missing", 0, ["pod has no id"]),
        cleanup.StagedPodArtifacts("pod-ok", "ok", 3, []),
        cleanup.StagedPodArtifacts("pod-fail", "", 0, ["storage unavailable"]),
    ]


def test_client_configuration_never_places_credentials_in_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def client(service: str, **kwargs: Any) -> object:
        captured.update(service=service, **kwargs)
        return object()

    monkeypatch.setattr("boto3.client", client)
    result = cleanup._r2_client(
        "https://account.r2.cloudflarestorage.com",
        "access",
        "secret",
        timeout_s=7,
    )

    assert result is not None
    assert captured["service"] == "s3"
    assert captured["endpoint_url"] == "https://account.r2.cloudflarestorage.com"
    assert captured["aws_access_key_id"] == "access"
    assert captured["aws_secret_access_key"] == "secret"
    assert captured["config"].connect_timeout == 7
