from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from pitwall.r2_temp_credentials import (
    MAX_R2_TEMP_CREDENTIAL_TTL_S,
    CloudflareR2TempCredentialClient,
    R2TempCredentialConfigError,
    R2TempCredentialEnvConfig,
    R2TempCredentialError,
    mint_r2_temp_credentials,
    vend_r2_temp_credential_pod_env,
)


def test_cloudflare_client_posts_temp_credential_request() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["Authorization"]
        seen["json"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "success": True,
                "errors": [],
                "messages": [],
                "result": {
                    "accessKeyId": "tmp-access",
                    "secretAccessKey": "tmp-secret",
                    "sessionToken": "tmp-session",
                },
            },
        )

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )

    credentials = client.create(
        bucket="pitwall-staging",
        parent_access_key_id="parent-key",
        ttl_seconds=900,
        permission="object-read-write",
        prefixes=("debug-logs/",),
        issued_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
    )

    assert seen["path"] == "/client/v4/accounts/acct123/r2/temp-access-credentials"
    assert seen["auth"] == "Bearer cf-token"
    assert seen["json"]["parentAccessKeyId"] == "parent-key"
    assert seen["json"]["ttlSeconds"] == 900
    assert seen["json"]["prefixes"] == ["debug-logs/"]
    assert credentials.access_key_id == "tmp-access"
    assert credentials.session_token == "tmp-session"
    assert credentials.expires_at == datetime(2026, 5, 28, 12, 15, tzinfo=UTC)


def test_temp_credentials_are_rendered_as_session_scoped_pod_env() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "accessKeyId": "tmp-access",
                    "secretAccessKey": "tmp-secret",
                    "sessionToken": "tmp-session",
                },
            },
        )

    environ = {
        "R2_ENDPOINT": "https://acct123.r2.cloudflarestorage.com",
        "R2_BUCKET_STAGING": "pitwall-staging",
        "CLOUDFLARE_ACCOUNT_ID": "acct123",
        "CLOUDFLARE_API_TOKEN": "cf-token",
        "R2_PARENT_ACCESS_KEY_ID": "parent-key",
        "R2_TEMP_CREDENTIALS_ENABLED": "true",
        "R2_TEMP_CREDENTIAL_TTL_S": "900",
    }
    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )

    env = vend_r2_temp_credential_pod_env(
        environ=environ,
        client=client,
        issued_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
    )

    assert env == {
        "R2_ENDPOINT": "https://acct123.r2.cloudflarestorage.com",
        "R2_BUCKET_STAGING": "pitwall-staging",
        "AWS_ACCESS_KEY_ID": "tmp-access",
        "AWS_SECRET_ACCESS_KEY": "tmp-secret",
        "AWS_SESSION_TOKEN": "tmp-session",
        "AWS_DEFAULT_REGION": "auto",
        "R2_SESSION_TOKEN": "tmp-session",
        "R2_CREDENTIAL_TTL_SECONDS": "900",
        "R2_CREDENTIAL_EXPIRES_AT": "2026-05-28T12:15:00Z",
    }
    assert "R2_ACCESS_KEY" not in env
    assert "R2_SECRET_KEY" not in env


def test_env_config_auto_mode_is_empty_when_unconfigured() -> None:
    assert R2TempCredentialEnvConfig.from_env({}) is None


def test_env_config_required_mode_fails_closed() -> None:
    with pytest.raises(R2TempCredentialConfigError, match="missing"):
        R2TempCredentialEnvConfig.from_env({"R2_TEMP_CREDENTIALS_REQUIRED": "true"})


def test_env_config_rejects_invalid_ttl() -> None:
    with pytest.raises(R2TempCredentialConfigError, match="ttl_seconds"):
        R2TempCredentialEnvConfig.from_env(
            {
                "R2_ENDPOINT": "https://r2.example.test",
                "R2_BUCKET_STAGING": "pitwall-staging",
                "CLOUDFLARE_ACCOUNT_ID": "acct123",
                "CLOUDFLARE_API_TOKEN": "cf-token",
                "R2_PARENT_ACCESS_KEY_ID": "parent-key",
                "R2_TEMP_CREDENTIALS_ENABLED": "true",
                "R2_TEMP_CREDENTIAL_TTL_S": "0",
            }
        )


def test_ttl_validation_rejects_zero_ttl() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": {}})

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(R2TempCredentialConfigError, match="ttl_seconds"):
        client.create(
            bucket="test-bucket",
            parent_access_key_id="parent-key",
            ttl_seconds=0,
        )


def test_ttl_validation_rejects_negative_ttl() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": {}})

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(R2TempCredentialConfigError, match="ttl_seconds"):
        client.create(
            bucket="test-bucket",
            parent_access_key_id="parent-key",
            ttl_seconds=-100,
        )


def test_ttl_validation_rejects_excessive_ttl() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": {}})

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(R2TempCredentialConfigError, match="ttl_seconds"):
        client.create(
            bucket="test-bucket",
            parent_access_key_id="parent-key",
            ttl_seconds=MAX_R2_TEMP_CREDENTIAL_TTL_S + 1,
        )


def test_ttl_validation_accepts_max_allowed_ttl() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "accessKeyId": "tmp-access",
                    "secretAccessKey": "tmp-secret",
                    "sessionToken": "tmp-session",
                },
            },
        )

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    creds = client.create(
        bucket="test-bucket",
        parent_access_key_id="parent-key",
        ttl_seconds=MAX_R2_TEMP_CREDENTIAL_TTL_S,
    )
    assert seen["json"]["ttlSeconds"] == 604800
    assert creds.ttl_seconds == MAX_R2_TEMP_CREDENTIAL_TTL_S


def test_prefix_scope_includes_single_prefix() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "accessKeyId": "tmp-access",
                    "secretAccessKey": "tmp-secret",
                    "sessionToken": "tmp-session",
                },
            },
        )

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    creds = client.create(
        bucket="test-bucket",
        parent_access_key_id="parent-key",
        prefixes=("uploads/2026/",),
    )
    assert seen["json"]["prefixes"] == ["uploads/2026/"]
    assert creds.prefixes == ("uploads/2026/",)


def test_prefix_scope_includes_multiple_prefixes() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "accessKeyId": "tmp-access",
                    "secretAccessKey": "tmp-secret",
                    "sessionToken": "tmp-session",
                },
            },
        )

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    creds = client.create(
        bucket="test-bucket",
        parent_access_key_id="parent-key",
        prefixes=("prefix-a/", "prefix-b/", "prefix-c/"),
    )
    assert seen["json"]["prefixes"] == ["prefix-a/", "prefix-b/", "prefix-c/"]
    assert creds.prefixes == ("prefix-a/", "prefix-b/", "prefix-c/")


def test_prefix_scope_filters_empty_strings() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "accessKeyId": "tmp-access",
                    "secretAccessKey": "tmp-secret",
                    "sessionToken": "tmp-session",
                },
            },
        )

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    creds = client.create(
        bucket="test-bucket",
        parent_access_key_id="parent-key",
        prefixes=("valid/", "", "   ", "also-valid/"),
    )
    assert seen["json"]["prefixes"] == ["valid/", "also-valid/"]
    assert creds.prefixes == ("valid/", "also-valid/")


def test_prefix_scope_omitted_when_empty() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "accessKeyId": "tmp-access",
                    "secretAccessKey": "tmp-secret",
                    "sessionToken": "tmp-session",
                },
            },
        )

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    creds = client.create(
        bucket="test-bucket",
        parent_access_key_id="parent-key",
        prefixes=(),
    )
    assert "prefixes" not in seen["json"]
    assert creds.prefixes == ()


def test_failure_handling_http_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(R2TempCredentialError, match="HTTP 500"):
        client.create(
            bucket="test-bucket",
            parent_access_key_id="parent-key",
        )


def test_failure_handling_non_success_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": False,
                "errors": [{"code": 1000, "message": "Invalid token"}],
            },
        )

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(R2TempCredentialError, match="unsuccessful"):
        client.create(
            bucket="test-bucket",
            parent_access_key_id="parent-key",
        )


def test_failure_handling_missing_result_fields() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "accessKeyId": "tmp-access",
                },
            },
        )

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(R2TempCredentialError, match="omitted required fields"):
        client.create(
            bucket="test-bucket",
            parent_access_key_id="parent-key",
        )


def test_failure_handling_invalid_json_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(R2TempCredentialError, match="not JSON"):
        client.create(
            bucket="test-bucket",
            parent_access_key_id="parent-key",
        )


def test_mint_r2_temp_credentials_creates_credentials() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "accessKeyId": "mint-access",
                    "secretAccessKey": "mint-secret",
                    "sessionToken": "mint-session",
                },
            },
        )

    client = CloudflareR2TempCredentialClient(
        account_id="acct123",
        api_token="cf-token",
        transport=httpx.MockTransport(handler),
    )
    creds = mint_r2_temp_credentials(
        bucket="test-bucket",
        parent_access_key_id="parent-key",
        account_id="acct123",
        api_token="cf-token",
        ttl_seconds=3600,
        prefixes=("prefix/",),
        client=client,
    )
    assert seen["path"] == "/client/v4/accounts/acct123/r2/temp-access-credentials"
    assert seen["auth"] == "Bearer cf-token"
    assert creds.access_key_id == "mint-access"
    assert creds.secret_access_key == "mint-secret"
    assert creds.session_token == "mint-session"
    assert creds.ttl_seconds == 3600
    assert creds.prefixes == ("prefix/",)
