"""RunPod template lifecycle — create once per image SHA, cache locally.

Template helpers create + cache RunPod templates so that repeated launches
reuse the same template rather than recreating it each time.

Also provides get/update/delete for managing existing templates and Hub
(public marketplace) template discovery for capabilities to reuse curated
pod+endpoint templates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any

import asyncpg
from pydantic import BaseModel, Field

from pitwall.runpod_client.graphql import (
    RUNPOD_GRAPHQL_URL,
    RunpodGraphQLClient,
    RunpodGraphQLError,
    RunpodGraphQLResponseError,
)
from pitwall.runpod_client.pods import _sdk
from pitwall.runpod_client.registry import registry_auth_id_from_env

log = logging.getLogger("pitwall.runpod_client.templates")


TEMPLATE_NAME = "pitwall-cloud-worker"

_TEMPLATE_ENV_KEYS = (
    "REDIS_URL",
    "PITWALL_CAPABILITY",
    "PITWALL_CAPABILITY_ID",
    "PITWALL_CAPABILITY_NAME",
    "PITWALL_PROVIDER",
    "PITWALL_PROVIDER_ID",
    "PITWALL_PROVIDER_NAME",
    "PITWALL_PROVIDER_TYPE",
    "PITWALL_REQUEST_ID",
    "VLLM_MODEL",
    "R2_ENDPOINT",
    "R2_BUCKET_STAGING",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_DEFAULT_REGION",
    "R2_SESSION_TOKEN",
    "R2_CREDENTIAL_TTL_SECONDS",
    "R2_CREDENTIAL_EXPIRES_AT",
)
_GRAPHQL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def image_sha(image_ref: str) -> str:
    """Extract the tag/digest portion of ``repo:tag`` or ``repo@sha256:...``."""
    if "@sha256:" in image_ref:
        return image_ref.split("@sha256:")[-1]
    if ":" in image_ref.rsplit("/", 1)[-1]:
        return image_ref.rsplit(":", 1)[-1]
    return "latest"


def template_suffix(image_ref: str) -> str:
    """Stable short suffix that avoids collisions for long smoke tags."""
    return hashlib.sha256(image_ref.encode("utf-8")).hexdigest()[:12]


def normalize_template_name(name: str) -> str:
    """Return a RunPod-friendly stable template name."""
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip()).strip("-")
    return normalized or TEMPLATE_NAME


def template_display_name(template_name: str, image_ref: str) -> str:
    """Return the visible RunPod "My Templates" name for this image."""
    return f"{normalize_template_name(template_name)}-{template_suffix(image_ref)}"


def _is_duplicate_template_name_error(exc: BaseException) -> bool:
    """True if a create_template failure is a RunPod template-name collision.

    RunPod rejects a duplicate display name with a "Template name must be unique"
    GraphQL error (surfaced as runpod.error.QueryError). Matched on the message
    rather than the type so SDK-version phrasing differences still classify.
    """
    message = str(exc).lower()
    return "unique" in message and "name" in message


def _resolve_existing_template_id(
    pod_templates: list[dict[str, Any]], display_name: str
) -> str | None:
    """Return the RunPod template id whose name == display_name, else None."""
    for entry in pod_templates:
        if not isinstance(entry, dict):
            continue
        if entry.get("name") == display_name and entry.get("id"):
            return str(entry["id"])
    return None


def _sdk_kwargs(api_key: str | None) -> dict[str, str]:
    return {"api_key": api_key} if api_key is not None else {}


def _custom_graphql_url(graphql_url: str | None) -> str | None:
    if graphql_url is None:
        return None
    normalized = graphql_url.rstrip("/")
    if normalized == RUNPOD_GRAPHQL_URL.rstrip("/"):
        return None
    return normalized


def _list_my_templates(api_key: str | None = None) -> list[dict[str, Any]]:
    """Return the account's RunPod pod templates as ``[{id, name}, ...]``.

    The runpod SDK exposes no list-templates call, so query GraphQL directly.
    ``_sdk()`` sets the module-level ``runpod.api_key`` that ``run_graphql_query``
    reads. Blocking (uses ``requests``); callers should run it in a thread.
    """
    _sdk(**_sdk_kwargs(api_key))
    import runpod.api.graphql as rp_graphql  # type: ignore[import-untyped]  # reason: no stubs

    response = dict(rp_graphql.run_graphql_query("query { myself { podTemplates { id name } } }"))
    myself = _graphql_data(response).get("myself") or {}
    pod_templates = myself.get("podTemplates") or []
    return [entry for entry in pod_templates if isinstance(entry, dict)]


class TemplateEnvVar(BaseModel):
    """Key-value pair for a template environment variable."""

    key: str
    value: str


class Template(BaseModel):
    """A RunPod pod template.

    Attributes:
        id: RunPod template ID.
        name: Display name of the template.
        image_name: Docker image reference (e.g. ``ghcr.io/org/worker:tag``).
        docker_args: Command to start the Docker container.
        container_disk_in_gb: Container disk size in GB.
        volume_in_gb: Volume size in GB.
        volume_mount_path: Path where the volume is mounted.
        ports: Port mappings string (e.g. ``"8888/http,666/tcp"``).
        env: Environment variables set on the template.
        is_serverless: Whether this is a serverless template.
        is_public: Whether this template is publicly visible in the Hub.
        readme: Template description/markdown.
    """

    model_config = {"populate_by_name": True}

    id: str
    name: str
    image_name: str = Field(validation_alias="imageName")
    docker_args: str | None = Field(default=None, validation_alias="dockerArgs")
    container_disk_in_gb: int = Field(default=10, validation_alias="containerDiskInGb")
    volume_in_gb: int = Field(default=0, validation_alias="volumeInGb")
    volume_mount_path: str | None = Field(default=None, validation_alias="volumeMountPath")
    ports: str = Field(default="", validation_alias="ports")
    env: list[TemplateEnvVar] | None = Field(default=None, validation_alias="env")
    is_serverless: bool = Field(default=False, validation_alias="isServerless")
    is_public: bool = Field(default=False, validation_alias="isPublic")
    readme: str = Field(default="", validation_alias="readme")


class HubTemplate(BaseModel):
    """A public Hub (marketplace) RunPod template.

    Hub templates are read-only community-curated templates that capabilities
    can reuse when launching pods or endpoints.

    Attributes:
        id: RunPod template ID.
        name: Display name of the template.
        image_name: Docker image reference.
        description: Short description of the template.
        github_url: Link to the template's source repository.
        docker_args: Command to start the Docker container.
        container_disk_in_gb: Container disk size in GB.
        volume_in_gb: Volume size in GB.
        volume_mount_path: Path where the volume is mounted.
        ports: Port mappings string.
        env: Environment variables set on the template.
        is_serverless: Whether this is a serverless template.
        display_name: User-facing name shown in the Hub UI.
        template_description: Longer description or README content.
    """

    model_config = {"populate_by_name": True}

    id: str
    name: str
    image_name: str = Field(validation_alias="imageName")
    description: str | None = Field(default=None, validation_alias="description")
    github_url: str | None = Field(default=None, validation_alias="githubUrl")
    docker_args: str | None = Field(default=None, validation_alias="dockerArgs")
    container_disk_in_gb: int = Field(default=10, validation_alias="containerDiskInGb")
    volume_in_gb: int = Field(default=0, validation_alias="volumeInGb")
    volume_mount_path: str | None = Field(default=None, validation_alias="volumeMountPath")
    ports: str = Field(default="", validation_alias="ports")
    env: list[TemplateEnvVar] | None = Field(default=None, validation_alias="env")
    is_serverless: bool = Field(default=False, validation_alias="isServerless")
    display_name: str | None = Field(default=None, validation_alias="displayName")
    template_description: str | None = Field(default=None, validation_alias="templateDescription")


def _run_graphql(query: str, *, api_key: str | None = None) -> dict[str, Any]:
    """Execute a GraphQL query/mutation and return the response data.

    Uses ``runpod.api_key`` set by ``_sdk()``. Blocking; callers should run
    in a thread.
    """
    _sdk(**_sdk_kwargs(api_key))
    import runpod.api.graphql as rp_graphql

    response: Any = rp_graphql.run_graphql_query(query)
    return dict(response)


def _graphql_api_key(api_key: str | None) -> str:
    return api_key if api_key is not None else os.environ.get("RUNPOD_API_KEY", "")


async def _run_graphql_client(
    query: str,
    *,
    api_key: str | None,
    graphql_url: str,
) -> dict[str, Any]:
    client = RunpodGraphQLClient(
        api_key=_graphql_api_key(api_key),
        graphql_url=graphql_url,
    )
    try:
        return await client._graphql(query)
    finally:
        await client.aclose()


def _graphql_string(value: str) -> str:
    return json.dumps(value)


def _graphql_id(value: str, *, field_name: str) -> str:
    if not _GRAPHQL_ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} contains invalid characters")
    return _graphql_string(value)


def _graphql_data(envelope: dict[str, Any]) -> dict[str, Any]:
    raw_errors = envelope.get("errors")
    if isinstance(raw_errors, list) and raw_errors:
        errors: list[dict[str, Any]] = []
        for raw_error in raw_errors:
            if isinstance(raw_error, dict):
                errors.append(raw_error)
            else:
                errors.append({"message": str(raw_error)})
        raise RunpodGraphQLError(errors)

    data = envelope.get("data")
    if not isinstance(data, dict):
        raise RunpodGraphQLResponseError("RunPod GraphQL response missing data object")
    return data


def _template_selection() -> str:
    """GraphQL fragment for full template fields."""
    return """id name imageName dockerArgs containerDiskInGb volumeInGb
    volumeMountPath ports env { key value } isServerless isPublic readme"""


def _hub_template_selection() -> str:
    """GraphQL fragment for community template fields.

    RunPod removed the ``hubPodTemplates`` surface (and its ``description``,
    ``githubUrl``, ``displayName``, ``templateDescription`` fields) from the
    GraphQL schema in 2026-07; community templates are now served by
    ``podTemplates``/``podTemplate`` with this reduced selection.
    """
    return """id name imageName dockerArgs
    containerDiskInGb volumeInGb volumeMountPath ports env { key value }
    isServerless"""


def _save_template_mutation(
    *,
    name: str,
    image_name: str,
    container_disk_in_gb: int,
    volume_mount_path: str | None,
    env: dict[str, str] | None,
    is_serverless: bool,
    registry_auth_id: str | None,
) -> str:
    env_items = ", ".join(
        f"{{ key: {_graphql_string(key)}, value: {_graphql_string(value)} }}"
        for key, value in (env or {}).items()
    )
    input_fields = [
        f"name: {_graphql_string(name)}",
        f"imageName: {_graphql_string(image_name)}",
        'dockerArgs: ""',
        f"containerDiskInGb: {container_disk_in_gb}",
        "volumeInGb: 0",
        'ports: ""',
        f"env: [{env_items}]" if env_items else "env: []",
        f"isServerless: {str(is_serverless).lower()}",
        f"containerRegistryAuthId: {_graphql_string(registry_auth_id or '')}",
        "startSsh: true",
        "isPublic: false",
        'readme: ""',
    ]
    if volume_mount_path is not None:
        input_fields.insert(5, f"volumeMountPath: {_graphql_string(volume_mount_path)}")

    input_block = ", ".join(input_fields)
    return f"""mutation {{
        saveTemplate(input: {{{input_block}}}) {{
            id
            name
            imageName
            dockerArgs
            containerDiskInGb
            volumeInGb
            volumeMountPath
            ports
            env {{
                key
                value
            }}
            isServerless
        }}
    }}"""


async def _create_template_with_graphql_client(
    *,
    name: str,
    image_name: str,
    container_disk_in_gb: int,
    volume_mount_path: str | None,
    env: dict[str, str] | None,
    is_serverless: bool,
    registry_auth_id: str | None,
    api_key: str | None,
    graphql_url: str,
) -> dict[str, Any]:
    data = await _run_graphql_client(
        _save_template_mutation(
            name=name,
            image_name=image_name,
            container_disk_in_gb=container_disk_in_gb,
            volume_mount_path=volume_mount_path,
            env=env,
            is_serverless=is_serverless,
            registry_auth_id=registry_auth_id,
        ),
        api_key=api_key,
        graphql_url=graphql_url,
    )
    template_data = data.get("saveTemplate")
    if not isinstance(template_data, dict):
        raise RunpodGraphQLResponseError("RunPod GraphQL response missing saveTemplate object")
    return template_data


async def _list_my_templates_for_auth(
    *,
    api_key: str | None,
    graphql_url: str | None,
) -> list[dict[str, Any]]:
    custom_url = _custom_graphql_url(graphql_url)
    if custom_url is None:
        if api_key is None:
            return await asyncio.to_thread(_list_my_templates)
        return await asyncio.to_thread(_list_my_templates, api_key)

    data = await _run_graphql_client(
        "query { myself { podTemplates { id name } } }",
        api_key=api_key,
        graphql_url=custom_url,
    )
    myself = data.get("myself") or {}
    pod_templates = myself.get("podTemplates") if isinstance(myself, dict) else []
    return [entry for entry in (pod_templates or []) if isinstance(entry, dict)]


async def get_template(template_id: str) -> Template:
    """Fetch a single RunPod template by ID.

    Args:
        template_id: The RunPod template ID.

    Returns:
        Template populated with the current RunPod template data.

    Raises:
        RuntimeError: If the template is not found or the GraphQL call fails.
    """
    query = f"""query {{
        template(id: {_graphql_id(template_id, field_name="template_id")}) {{
            {_template_selection()}
        }}
    }}"""
    result = await asyncio.to_thread(_run_graphql, query)
    data = _graphql_data(result)
    template_data = data.get("template")
    if not template_data:
        raise TemplateNotFoundError(f"Template {template_id!r} not found")
    return Template.model_validate(template_data)


class TemplateNotFoundError(RuntimeError):
    """Raised when a RunPod template ID does not exist."""


class TemplateDeleteError(RuntimeError):
    """Raised when a RunPod template cannot be deleted."""


async def update_template(
    template_id: str,
    *,
    name: str | None = None,
    image_name: str | None = None,
    docker_args: str | None = None,
    container_disk_in_gb: int | None = None,
    volume_in_gb: int | None = None,
    volume_mount_path: str | None = None,
    ports: str | None = None,
    env: dict[str, str] | None = None,
    is_serverless: bool | None = None,
    is_public: bool | None = None,
    readme: str | None = None,
) -> Template:
    """Update a RunPod template's properties.

    Args:
        template_id: The RunPod template ID to update.
        name: New display name for the template.
        image_name: New Docker image reference.
        docker_args: New container start command.
        container_disk_in_gb: New container disk size in GB.
        volume_in_gb: New volume size in GB.
        volume_mount_path: New volume mount path.
        ports: New port mappings string.
        env: New environment variables dict (replaces existing).
        is_serverless: Whether this is a serverless template.
        is_public: Whether this template is publicly visible.
        readme: New template description/markdown.

    Returns:
        Template populated with the updated RunPod template data.

    Raises:
        TemplateNotFoundError: If the template ID does not exist.
    """
    input_parts: list[str] = []
    if name is not None:
        input_parts.append(f"name: {_graphql_string(name)}")
    if image_name is not None:
        input_parts.append(f"imageName: {_graphql_string(image_name)}")
    if docker_args is not None:
        input_parts.append(f"dockerArgs: {_graphql_string(docker_args)}")
    if container_disk_in_gb is not None:
        input_parts.append(f"containerDiskInGb: {container_disk_in_gb}")
    if volume_in_gb is not None:
        input_parts.append(f"volumeInGb: {volume_in_gb}")
    if volume_mount_path is not None:
        input_parts.append(f"volumeMountPath: {_graphql_string(volume_mount_path)}")
    if ports is not None:
        input_parts.append(f"ports: {_graphql_string(ports)}")
    if env is not None:
        env_string = ", ".join(
            f"{{ key: {_graphql_string(k)}, value: {_graphql_string(v)} }}" for k, v in env.items()
        )
        input_parts.append(f"env: [{env_string}]")
    if is_serverless is not None:
        input_parts.append(f"isServerless: {str(is_serverless).lower()}")
    if is_public is not None:
        input_parts.append(f"isPublic: {str(is_public).lower()}")
    if readme is not None:
        input_parts.append(f"readme: {_graphql_string(readme)}")

    input_block = ", ".join(input_parts) if input_parts else ""
    mutation = f"""mutation {{
        updateTemplate(id: {_graphql_id(template_id, field_name="template_id")}, input: {{{input_block}}}) {{
            {_template_selection()}
        }}
    }}"""
    result = await asyncio.to_thread(_run_graphql, mutation)
    data = _graphql_data(result)
    template_data = data.get("updateTemplate")
    if not template_data:
        raise TemplateNotFoundError(f"Template {template_id!r} not found or update failed")
    return Template.model_validate(template_data)


async def delete_template(template_id: str) -> bool:
    """Delete a RunPod template by ID.

    Args:
        template_id: The RunPod template ID to delete.

    Returns:
        True if deletion was successful.

    Raises:
        TemplateDeleteError: If the template cannot be deleted (e.g. not found,
            in use by active endpoints).
    """
    mutation = f"""mutation {{
        deleteTemplate(id: {_graphql_id(template_id, field_name="template_id")})
    }}"""
    try:
        result = await asyncio.to_thread(_run_graphql, mutation)
    except Exception as exc:  # reason: wrap any GraphQL/transport failure in TemplateDeleteError
        raise TemplateDeleteError(f"Failed to delete template {template_id!r}: {exc}") from exc
    data = _graphql_data(result)
    deleted = data.get("deleteTemplate")
    if not deleted:
        raise TemplateDeleteError(f"Template {template_id!r} could not be deleted")
    return True


async def list_hub_templates(
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[HubTemplate]:
    """List public Hub (marketplace) RunPod templates.

    Hub templates are community-curated templates that capabilities can reuse
    for pod or endpoint launches without creating their own.

    Args:
        limit: Maximum number of templates to return (default 50, max 100).
        offset: Number of templates to skip for pagination.

    Returns:
        List of HubTemplate objects.
    """
    clamped_limit = min(max(1, limit), 100)
    # podTemplates takes no pagination arguments; slice client-side.
    query = f"""query {{
        podTemplates {{
            {_hub_template_selection()}
        }}
    }}"""
    result = await asyncio.to_thread(_run_graphql, query)
    data = _graphql_data(result)
    templates_data = data.get("podTemplates") or []
    page = templates_data[offset : offset + clamped_limit]
    return [HubTemplate.model_validate(t) for t in page if isinstance(t, dict)]


async def get_hub_template(template_id: str) -> HubTemplate:
    """Fetch a single public Hub template by ID.

    Args:
        template_id: The RunPod Hub template ID.

    Returns:
        HubTemplate populated with the template data.

    Raises:
        TemplateNotFoundError: If the Hub template does not exist.
    """
    query = f"""query {{
        podTemplate(id: {_graphql_id(template_id, field_name="template_id")}) {{
            {_hub_template_selection()}
        }}
    }}"""
    result = await asyncio.to_thread(_run_graphql, query)
    data = _graphql_data(result)
    template_data = data.get("podTemplate")
    if not template_data:
        raise TemplateNotFoundError(f"Hub template {template_id!r} not found")
    return HubTemplate.model_validate(template_data)


async def _lookup_cached(pool: asyncpg.Pool, name: str, sha: str) -> str | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT runpod_template_id FROM runpod_templates WHERE name=$1 AND image_sha=$2",
            name,
            sha,
        )
    return row["runpod_template_id"] if row else None


async def _insert_cache(
    pool: asyncpg.Pool,
    *,
    template_id: str,
    name: str,
    sha: str,
    image_ref: str,
    registry_auth_id: str | None,
    container_disk_gb: int = 50,
    volume_mount_path: str = "/workspace",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO runpod_templates
                (id, runpod_template_id, name, image_sha, image_ref, registry_auth_id, container_disk_gb, volume_mount_path)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (name, image_sha) DO NOTHING""",
            template_id,
            template_id,
            name,
            sha,
            image_ref,
            registry_auth_id,
            container_disk_gb,
            volume_mount_path,
        )


async def ensure_template(
    pool: asyncpg.Pool,
    image_ref: str,
    *,
    template_name: str = TEMPLATE_NAME,
    registry_auth_id: str | None = None,
    container_disk_gb: int = 50,
    volume_mount_path: str = "/workspace",
    api_key: str | None = None,
    graphql_url: str | None = None,
) -> str:
    """Return the RunPod template_id for this image. Creates + caches if missing.

    ``image_ref``: full image path with tag or digest (e.g.
        ``ghcr.io/org/cloud-worker:abc123``).
    ``registry_auth_id``: RunPod's credential ID for the private-registry pull
        secret. Required for private registries; pass None for public images.
    """
    sha = image_sha(image_ref)
    logical_name = normalize_template_name(template_name)
    cached = await _lookup_cached(pool, logical_name, sha)
    if cached:
        log.debug(
            "template cache hit: name=%s sha=%s template_id=%s",
            logical_name,
            sha,
            cached,
        )
        return cached

    env_block = dict.fromkeys(_TEMPLATE_ENV_KEYS, "")

    display_name = template_display_name(logical_name, image_ref)
    log.info("creating RunPod template: name=%s image=%s", display_name, image_ref)
    custom_url = _custom_graphql_url(graphql_url)
    try:
        if custom_url is None:
            rp = _sdk(**_sdk_kwargs(api_key))
            result = await asyncio.to_thread(
                rp.create_template,
                name=display_name,
                image_name=image_ref,
                container_disk_in_gb=container_disk_gb,
                volume_mount_path=volume_mount_path,
                env=env_block,
                is_serverless=False,
                registry_auth_id=registry_auth_id,
            )
        else:
            result = await _create_template_with_graphql_client(
                name=display_name,
                image_name=image_ref,
                container_disk_in_gb=container_disk_gb,
                volume_mount_path=volume_mount_path,
                env=env_block,
                is_serverless=False,
                registry_auth_id=registry_auth_id,
                api_key=api_key,
                graphql_url=custom_url,
            )
    except Exception as exc:  # reason: RunPod rejects a duplicate display name; reuse the existing template instead of failing the launch
        if not _is_duplicate_template_name_error(exc):
            raise
        log.info(
            "template name collision on RunPod (DB cache miss); resolving existing: name=%s",
            display_name,
        )
        pod_templates = await _list_my_templates_for_auth(
            api_key=api_key,
            graphql_url=custom_url,
        )
        existing_id = _resolve_existing_template_id(pod_templates, display_name)
        if not existing_id:
            raise
        await _insert_cache(
            pool,
            template_id=existing_id,
            name=logical_name,
            sha=sha,
            image_ref=image_ref,
            registry_auth_id=registry_auth_id,
            container_disk_gb=container_disk_gb,
            volume_mount_path=volume_mount_path,
        )
        log.info(
            "reused existing RunPod template by name: name=%s id=%s sha=%s",
            display_name,
            existing_id,
            sha,
        )
        return existing_id

    template_id: str | None = None
    if isinstance(result, dict):
        template_id = result.get("id") or result.get("templateId")
    if not template_id:
        raise RuntimeError(f"runpod.create_template returned unexpected shape: {result!r}")

    await _insert_cache(
        pool,
        template_id=template_id,
        name=logical_name,
        sha=sha,
        image_ref=image_ref,
        registry_auth_id=registry_auth_id,
        container_disk_gb=container_disk_gb,
        volume_mount_path=volume_mount_path,
    )
    log.info(
        "template created + cached: name=%s id=%s sha=%s",
        display_name,
        template_id,
        sha,
    )
    return template_id


def get_image_ref_from_env() -> str:
    """Return the image ref that the launcher should use.

    Read from env var ``PITWALL_CLOUD_WORKER_IMAGE``. Fail fast if unset — we
    don't want to silently default to ``latest`` which could pick up an unvetted
    image.
    """
    ref = os.environ.get("PITWALL_CLOUD_WORKER_IMAGE")
    if not ref:
        raise RuntimeError("PITWALL_CLOUD_WORKER_IMAGE not set")
    return ref


def get_registry_auth_id_from_env(image_ref: str | None = None) -> str | None:
    """Return the RunPod registry-auth ID for the given image, or None if public.

    Picks credentials by image prefix: GHCR, GitLab Registry, and Docker Hub
    can each use separate RunPod credential IDs. The legacy
    ``RUNPOD_REGISTRY_AUTH_ID`` fallback remains for existing GHCR callers.
    """
    return registry_auth_id_from_env(image_ref)


__all__ = [
    "TEMPLATE_NAME",
    "Template",
    "TemplateEnvVar",
    "HubTemplate",
    "TemplateNotFoundError",
    "TemplateDeleteError",
    "image_sha",
    "normalize_template_name",
    "template_display_name",
    "template_suffix",
    "ensure_template",
    "get_template",
    "update_template",
    "delete_template",
    "list_hub_templates",
    "get_hub_template",
    "get_image_ref_from_env",
    "get_registry_auth_id_from_env",
]
