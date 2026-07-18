from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
import respx

from pitwall.runpod_client.registry import (
    DOCKER_HUB_PREFIX,
    GHCR_PREFIX,
    GITLAB_REGISTRY_PREFIX,
    ContainerRegistryAuth,
    RegistryAuthError,
    create_container_registry_auth,
    delete_container_registry_auth,
    get_container_registry_auth,
    image_registry_prefix,
    list_container_registry_auths,
    registry_auth_id_from_env,
)

AUTH_ID = "clzdaifot0001l90809257ynb"
AUTH_NAME = "my-creds"


# Existing tests ---------------------------------------------------------------


def test_image_registry_prefix_normalizes_supported_registries() -> None:
    assert image_registry_prefix("ghcr.io/org/worker:abc") == GHCR_PREFIX
    assert image_registry_prefix("registry.gitlab.com/org/worker:abc") == GITLAB_REGISTRY_PREFIX
    assert (
        image_registry_prefix("gitlab-registry.example.com/org/worker:abc")
        == GITLAB_REGISTRY_PREFIX
    )
    assert image_registry_prefix("vllm/vllm-openai:v0.11.2") == DOCKER_HUB_PREFIX


def test_registry_auth_id_selects_by_image_prefix() -> None:
    env = {
        "RUNPOD_REGISTRY_AUTH_ID": "legacy-auth",
        "RUNPOD_REGISTRY_AUTH_ID_GHCR": "ghcr-auth",
        "RUNPOD_REGISTRY_AUTH_ID_GITLAB": "gitlab-auth",
        "RUNPOD_REGISTRY_AUTH_ID_DOCKER_HUB": "docker-auth",
    }

    assert registry_auth_id_from_env("ghcr.io/org/worker:abc", environ=env) == "ghcr-auth"
    assert (
        registry_auth_id_from_env("registry.gitlab.com/org/worker:abc", environ=env)
        == "gitlab-auth"
    )
    assert registry_auth_id_from_env("vllm/vllm-openai:v0.11.2", environ=env) == "docker-auth"


def test_registry_auth_id_preserves_legacy_fallback() -> None:
    env = {"RUNPOD_REGISTRY_AUTH_ID": "legacy-auth"}

    assert registry_auth_id_from_env("ghcr.io/org/worker:abc", environ=env) == "legacy-auth"
    assert (
        registry_auth_id_from_env("registry.gitlab.com/org/worker:abc", environ=env)
        == "legacy-auth"
    )


# CRUD tests -------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_create_container_registry_auth_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_auth: list[str | None] = []
    captured_body: list[dict[str, Any]] = []
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        captured_auth.append(request.headers.get("authorization"))
        captured_body.append(cast(dict[str, Any], json.loads(request.content)))
        return httpx.Response(
            200,
            json={"id": AUTH_ID, "name": AUTH_NAME},
        )

    route = respx.post("https://rest.runpod.io/v1/containerregistryauth").mock(side_effect=handler)

    result = await create_container_registry_auth(
        name=AUTH_NAME,
        username="my-user",
        password="super-secret",
    )

    assert isinstance(result, ContainerRegistryAuth)
    assert result.id == AUTH_ID
    assert result.name == AUTH_NAME
    assert captured_auth == ["Bearer test-key"]
    assert captured_body == [{"name": AUTH_NAME, "username": "my-user", "password": "super-secret"}]
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_create_container_registry_auth_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.post("https://rest.runpod.io/v1/containerregistryauth").mock(
        return_value=httpx.Response(400, text='{"error":"name already exists"}')
    )

    with pytest.raises(RegistryAuthError) as exc_info:
        await create_container_registry_auth(
            name=AUTH_NAME,
            username="my-user",
            password="super-secret",
        )

    assert "400" in str(exc_info.value)
    assert "name already exists" in str(exc_info.value)


@respx.mock
@pytest.mark.asyncio
async def test_create_container_registry_auth_respects_rest_api_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("RUNPOD_REST_API_URL", "https://runpod.example.test/api/")
    route = respx.post("https://runpod.example.test/api/containerregistryauth").mock(
        return_value=httpx.Response(200, json={"id": AUTH_ID, "name": AUTH_NAME})
    )

    result = await create_container_registry_auth(
        name=AUTH_NAME,
        username="user",
        password="pass",
    )

    assert result.id == AUTH_ID
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_create_container_registry_auth_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

    with pytest.raises(RegistryAuthError, match="RUNPOD_API_KEY not set"):
        await create_container_registry_auth(
            name=AUTH_NAME,
            username="user",
            password="pass",
        )


@respx.mock
@pytest.mark.asyncio
async def test_create_container_registry_auth_unexpected_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.post("https://rest.runpod.io/v1/containerregistryauth").mock(
        return_value=httpx.Response(200, text="not json")
    )

    with pytest.raises(RegistryAuthError, match="non-JSON body"):
        await create_container_registry_auth(
            name=AUTH_NAME,
            username="user",
            password="pass",
        )


@respx.mock
@pytest.mark.asyncio
async def test_get_container_registry_auth_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_auth: list[str | None] = []
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        captured_auth.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"id": AUTH_ID, "name": AUTH_NAME})

    route = respx.get(f"https://rest.runpod.io/v1/containerregistryauth/{AUTH_ID}").mock(
        side_effect=handler
    )

    result = await get_container_registry_auth(AUTH_ID)

    assert isinstance(result, ContainerRegistryAuth)
    assert result.id == AUTH_ID
    assert result.name == AUTH_NAME
    assert captured_auth == ["Bearer test-key"]
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_get_container_registry_auth_not_found_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.get(f"https://rest.runpod.io/v1/containerregistryauth/{AUTH_ID}").mock(
        return_value=httpx.Response(404, text='{"error":"not found"}')
    )

    result = await get_container_registry_auth(AUTH_ID)

    assert result is None


@respx.mock
@pytest.mark.asyncio
async def test_get_container_registry_auth_error_5xx_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.get(f"https://rest.runpod.io/v1/containerregistryauth/{AUTH_ID}").mock(
        return_value=httpx.Response(500, text='{"error":"internal"}')
    )

    with pytest.raises(RegistryAuthError) as exc_info:
        await get_container_registry_auth(AUTH_ID)

    assert "500" in str(exc_info.value)


@respx.mock
@pytest.mark.asyncio
async def test_get_container_registry_auth_500_not_found_text_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.get(f"https://rest.runpod.io/v1/containerregistryauth/{AUTH_ID}").mock(
        return_value=httpx.Response(500, text='{"error":"upstream dependency not found"}')
    )

    with pytest.raises(RegistryAuthError) as exc_info:
        await get_container_registry_auth(AUTH_ID)

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_get_container_registry_auth_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

    with pytest.raises(RegistryAuthError, match="RUNPOD_API_KEY not set"):
        await get_container_registry_auth(AUTH_ID)


@respx.mock
@pytest.mark.asyncio
async def test_list_container_registry_auths_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_auth: list[str | None] = []
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        captured_auth.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            json=[
                {"id": AUTH_ID, "name": AUTH_NAME},
                {"id": "auth-2", "name": "other-creds"},
            ],
        )

    route = respx.get("https://rest.runpod.io/v1/containerregistryauth").mock(side_effect=handler)

    result = await list_container_registry_auths()

    assert len(result) == 2
    assert all(isinstance(item, ContainerRegistryAuth) for item in result)
    assert result[0].id == AUTH_ID
    assert result[0].name == AUTH_NAME
    assert result[1].id == "auth-2"
    assert captured_auth == ["Bearer test-key"]
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_list_container_registry_auths_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.get("https://rest.runpod.io/v1/containerregistryauth").mock(
        return_value=httpx.Response(200, json=[])
    )

    result = await list_container_registry_auths()

    assert result == []


@respx.mock
@pytest.mark.asyncio
async def test_list_container_registry_auths_unexpected_shape_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.get("https://rest.runpod.io/v1/containerregistryauth").mock(
        return_value=httpx.Response(200, json={"id": AUTH_ID, "name": AUTH_NAME})
    )

    with pytest.raises(RegistryAuthError, match="unexpected shape"):
        await list_container_registry_auths()


@respx.mock
@pytest.mark.asyncio
async def test_list_container_registry_auths_error_5xx_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.get("https://rest.runpod.io/v1/containerregistryauth").mock(
        return_value=httpx.Response(503, text='{"error":"unavailable"}')
    )

    with pytest.raises(RegistryAuthError) as exc_info:
        await list_container_registry_auths()

    assert "503" in str(exc_info.value)


@pytest.mark.asyncio
async def test_list_container_registry_auths_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

    with pytest.raises(RegistryAuthError, match="RUNPOD_API_KEY not set"):
        await list_container_registry_auths()


@respx.mock
@pytest.mark.asyncio
async def test_delete_container_registry_auth_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_auth: list[str | None] = []
    captured_methods: list[str] = []
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        captured_auth.append(request.headers.get("authorization"))
        captured_methods.append(request.method)
        return httpx.Response(204)

    route = respx.delete(f"https://rest.runpod.io/v1/containerregistryauth/{AUTH_ID}").mock(
        side_effect=handler
    )

    await delete_container_registry_auth(AUTH_ID)

    assert captured_auth == ["Bearer test-key"]
    assert captured_methods == ["DELETE"]
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_delete_container_registry_auth_idempotent_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.delete(f"https://rest.runpod.io/v1/containerregistryauth/{AUTH_ID}").mock(
        return_value=httpx.Response(404, text='{"error":"not found"}')
    )

    await delete_container_registry_auth(AUTH_ID)


@respx.mock
@pytest.mark.asyncio
async def test_delete_container_registry_auth_error_5xx_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.delete(f"https://rest.runpod.io/v1/containerregistryauth/{AUTH_ID}").mock(
        return_value=httpx.Response(500, text='{"error":"internal"}')
    )

    with pytest.raises(RegistryAuthError) as exc_info:
        await delete_container_registry_auth(AUTH_ID)

    assert "500" in str(exc_info.value)


@respx.mock
@pytest.mark.asyncio
async def test_delete_container_registry_auth_403_not_found_text_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    respx.delete(f"https://rest.runpod.io/v1/containerregistryauth/{AUTH_ID}").mock(
        return_value=httpx.Response(403, text='{"error":"credential not found for tenant"}')
    )

    with pytest.raises(RegistryAuthError) as exc_info:
        await delete_container_registry_auth(AUTH_ID)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_container_registry_auth_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

    with pytest.raises(RegistryAuthError, match="RUNPOD_API_KEY not set"):
        await delete_container_registry_auth(AUTH_ID)
