"""RunPod registry-auth selection helpers and container-registry credential CRUD.

RunPod stores registry credentials as auth IDs. Pitwall selects the auth ID
from the image reference prefix so GHCR, GitLab Registry, and Docker Hub can be
configured independently.

The CRUD surface manages container-registry credentials via RunPod's REST API
at ``https://rest.runpod.io/v1``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import BaseModel

LEGACY_REGISTRY_AUTH_ENV = "RUNPOD_REGISTRY_AUTH_ID"
GHCR_REGISTRY_AUTH_ENV = "RUNPOD_REGISTRY_AUTH_ID_GHCR"
GITLAB_REGISTRY_AUTH_ENV = "RUNPOD_REGISTRY_AUTH_ID_GITLAB"
DOCKER_HUB_REGISTRY_AUTH_ENV = "RUNPOD_REGISTRY_AUTH_ID_DOCKER_HUB"

GHCR_PREFIX = "ghcr.io"
GITLAB_REGISTRY_PREFIX = "registry.gitlab.com"
DOCKER_HUB_PREFIX = "docker.io"

_REGISTRY_AUTH_PATH = "containerregistryauth"
_REGISTRY_BASE_URL = "https://rest.runpod.io/v1"


class RegistryAuthError(RuntimeError):
    """Base error for registry-auth CRUD failures."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RegistryAuthCreateInput(BaseModel):
    """Input for creating a container-registry auth credential.

    Attributes:
        name: User-defined name; must be unique across the RunPod account.
        username: Registry username.
        password: Registry password or access token.
    """

    name: str
    username: str
    password: str


class ContainerRegistryAuth(BaseModel):
    """A container-registry auth credential as returned by RunPod.

    RunPod never returns the ``username`` or ``password`` after creation;
    only ``id`` and ``name`` are available from the API.
    """

    id: str
    name: str


def _registry_auth_url(path: str | None = None) -> str:
    base = os.environ.get("RUNPOD_REST_API_URL", _REGISTRY_BASE_URL).rstrip("/")
    if path is None:
        return f"{base}/{_REGISTRY_AUTH_PATH}"
    return f"{base}/{_REGISTRY_AUTH_PATH}/{path.lstrip('/')}"


def _registry_auth_headers() -> dict[str, str]:
    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        raise RegistryAuthError("RUNPOD_API_KEY not set in process env")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _rest_request_registry_auth(
    method: str,
    path: str | None = None,
    *,
    json_body: dict[str, Any] | None = None,
    timeout_s: float = 60.0,
) -> Any:
    url = _registry_auth_url(path)
    with httpx.Client(timeout=timeout_s) as client:
        response = client.request(
            method,
            url,
            headers=_registry_auth_headers(),
            json=json_body,
        )
    if response.status_code == 204:
        return None
    if response.status_code >= 400:
        raise RegistryAuthError(
            f"{method} {url} failed with HTTP {response.status_code}: {response.text}",
            status_code=response.status_code,
        )
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:  # includes JSONDecodeError
        raise RegistryAuthError(
            f"{method} {url} returned non-JSON body: {response.text!r}"
        ) from exc


async def create_container_registry_auth(
    name: str,
    username: str,
    password: str,
    *,
    timeout_s: float = 60.0,
) -> ContainerRegistryAuth:
    """Create a container-registry auth credential.

    Args:
        name: User-defined name; must be unique in the RunPod account.
        username: Registry username.
        password: Registry password or access token.
        timeout_s: Request timeout in seconds.

    Returns:
        ContainerRegistryAuth with the assigned ``id`` and provided ``name``.

    Raises:
        RegistryAuthError: On HTTP 4xx/5xx or missing API key.
    """
    payload: dict[str, str] = {"name": name, "username": username, "password": password}
    result = await asyncio.to_thread(
        _rest_request_registry_auth,
        "POST",
        json_body=payload,
        timeout_s=timeout_s,
    )
    if not isinstance(result, dict):
        raise RegistryAuthError(
            f"create_container_registry_auth returned unexpected shape: {result!r}"
        )
    return ContainerRegistryAuth.model_validate(result)


async def get_container_registry_auth(
    auth_id: str,
    *,
    timeout_s: float = 60.0,
) -> ContainerRegistryAuth | None:
    """Fetch a single container-registry auth by its RunPod ``auth_id``.

    Args:
        auth_id: RunPod-assigned auth ID.
        timeout_s: Request timeout in seconds.

    Returns:
        ContainerRegistryAuth if found; ``None`` if the server returns 404.

    Raises:
        RegistryAuthError: On HTTP 5xx or missing API key.
    """
    try:
        result = await asyncio.to_thread(
            _rest_request_registry_auth,
            "GET",
            auth_id,
            timeout_s=timeout_s,
        )
    except RegistryAuthError as exc:
        if exc.status_code == 404:
            return None
        raise
    if not isinstance(result, dict):
        raise RegistryAuthError(
            f"get_container_registry_auth returned unexpected shape: {result!r}"
        )
    return ContainerRegistryAuth.model_validate(result)


async def list_container_registry_auths(
    *,
    timeout_s: float = 60.0,
) -> list[ContainerRegistryAuth]:
    """List all container-registry auths for the RunPod account.

    Args:
        timeout_s: Request timeout in seconds.

    Returns:
        List of ContainerRegistryAuth objects (may be empty).

    Raises:
        RegistryAuthError: On HTTP 5xx or missing API key.
    """
    result = await asyncio.to_thread(
        _rest_request_registry_auth,
        "GET",
        timeout_s=timeout_s,
    )
    if not isinstance(result, list):
        raise RegistryAuthError(
            f"list_container_registry_auths returned unexpected shape: {result!r}"
        )
    return [ContainerRegistryAuth.model_validate(item) for item in result]


async def delete_container_registry_auth(
    auth_id: str,
    *,
    timeout_s: float = 60.0,
) -> None:
    """Delete a container-registry auth credential by RunPod ``auth_id``.

    Idempotent: succeeds silently if the credential does not exist.

    Args:
        auth_id: RunPod-assigned auth ID to delete.
        timeout_s: Request timeout in seconds.

    Raises:
        RegistryAuthError: On HTTP 5xx or missing API key.
    """
    try:
        await asyncio.to_thread(
            _rest_request_registry_auth,
            "DELETE",
            auth_id,
            timeout_s=timeout_s,
        )
    except RegistryAuthError as exc:
        if exc.status_code == 404:
            return
        raise


def image_registry_prefix(image_ref: str | None) -> str | None:
    """Return the normalized registry prefix for an image reference."""

    if not image_ref:
        return None

    first = image_ref.split("/", 1)[0].lower()
    if first == GHCR_PREFIX:
        return GHCR_PREFIX
    if first == GITLAB_REGISTRY_PREFIX or first.startswith("gitlab-registry."):
        return GITLAB_REGISTRY_PREFIX
    if first in {DOCKER_HUB_PREFIX, "registry.hub.docker.com"}:
        return DOCKER_HUB_PREFIX

    # Docker Hub shorthand: ``library/python`` or ``vllm/vllm-openai``.
    if "." not in first and ":" not in first and first != "localhost":
        return DOCKER_HUB_PREFIX

    return first


def registry_auth_env_names_for_image_ref(image_ref: str | None) -> tuple[str, ...]:
    """Return auth env vars to try, in priority order, for *image_ref*."""

    prefix = image_registry_prefix(image_ref)
    if prefix == GHCR_PREFIX:
        return (GHCR_REGISTRY_AUTH_ENV, LEGACY_REGISTRY_AUTH_ENV)
    if prefix == GITLAB_REGISTRY_PREFIX:
        return (GITLAB_REGISTRY_AUTH_ENV, LEGACY_REGISTRY_AUTH_ENV)
    if prefix == DOCKER_HUB_PREFIX:
        return (DOCKER_HUB_REGISTRY_AUTH_ENV,)
    return (LEGACY_REGISTRY_AUTH_ENV,)


def registry_auth_id_from_env(
    image_ref: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return the configured RunPod registry-auth ID for *image_ref*.

    When no image ref is provided, the legacy GHCR-compatible env var is used
    for backwards compatibility with older callers.
    """

    env = os.environ if environ is None else environ
    if image_ref is None:
        return env.get(LEGACY_REGISTRY_AUTH_ENV) or env.get(GHCR_REGISTRY_AUTH_ENV) or None

    for env_name in registry_auth_env_names_for_image_ref(image_ref):
        auth_id = env.get(env_name)
        if auth_id:
            return auth_id
    return None


__all__ = [
    "ContainerRegistryAuth",
    "DOCKER_HUB_PREFIX",
    "DOCKER_HUB_REGISTRY_AUTH_ENV",
    "GHCR_PREFIX",
    "GHCR_REGISTRY_AUTH_ENV",
    "GITLAB_REGISTRY_AUTH_ENV",
    "GITLAB_REGISTRY_PREFIX",
    "LEGACY_REGISTRY_AUTH_ENV",
    "RegistryAuthCreateInput",
    "RegistryAuthError",
    "create_container_registry_auth",
    "delete_container_registry_auth",
    "get_container_registry_auth",
    "image_registry_prefix",
    "list_container_registry_auths",
    "registry_auth_env_names_for_image_ref",
    "registry_auth_id_from_env",
]
