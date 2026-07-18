"""RunPod volume mount path constants and network volume CRUD + S3 client.

L10: RunPod Pods mount network volumes at ``/workspace`` while Serverless
workers use ``/runpod-volume``. Keep the difference behind provider-type
constants so capability and workload callers do not carry mount paths.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import os
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field

from pitwall.core.enums import ProviderType
from pitwall.runpod_client.pods import RunPodError, RunPodRestError

log = logging.getLogger("pitwall.runpod_client.mounts")

POD_VOLUME_MOUNT_PATH = "/workspace"
SERVERLESS_VOLUME_MOUNT_PATH = "/runpod-volume"
_S3_DATA_CENTER_ID_RE = re.compile(r"(?!(?:\d+)$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)")

POD_MOUNT_PATH = POD_VOLUME_MOUNT_PATH
SERVERLESS_MOUNT_PATH = SERVERLESS_VOLUME_MOUNT_PATH

POD_PROVIDER_TYPES = frozenset({ProviderType.POD_LEASE})
SERVERLESS_PROVIDER_TYPES = frozenset(
    {
        ProviderType.SERVERLESS_QUEUE,
        ProviderType.SERVERLESS_LB,
        ProviderType.PUBLIC_ENDPOINT,
    }
)

PROVIDER_TYPE_VOLUME_MOUNT_PATHS: Mapping[ProviderType, str] = MappingProxyType(
    {
        ProviderType.POD_LEASE: POD_VOLUME_MOUNT_PATH,
        ProviderType.SERVERLESS_QUEUE: SERVERLESS_VOLUME_MOUNT_PATH,
        ProviderType.SERVERLESS_LB: SERVERLESS_VOLUME_MOUNT_PATH,
        ProviderType.PUBLIC_ENDPOINT: SERVERLESS_VOLUME_MOUNT_PATH,
    }
)
PROVIDER_TYPE_MOUNT_PATHS = PROVIDER_TYPE_VOLUME_MOUNT_PATHS


def provider_type_volume_mount_path(provider_type: ProviderType | str) -> str:
    """Return the canonical RunPod volume mount path for a provider type."""

    try:
        resolved_provider_type = ProviderType(provider_type)
    except ValueError as exc:
        raise ValueError(f"unknown provider_type {provider_type!r}") from exc
    return PROVIDER_TYPE_VOLUME_MOUNT_PATHS[resolved_provider_type]


mount_path_for_provider_type = provider_type_volume_mount_path


def _s3_data_center_id(dc: str) -> str:
    if not _S3_DATA_CENTER_ID_RE.fullmatch(dc):
        raise RunPodError(f"invalid RunPod data center ID for S3 endpoint: {dc!r}")
    return dc.lower()


class NetworkVolume(BaseModel):
    """RunPod network volume REST representation."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    size: int
    data_center_id: str = Field(alias="dataCenterId")


class S3Object(BaseModel):
    """S3 object metadata inside a RunPod network volume."""

    model_config = ConfigDict(populate_by_name=True)

    key: str
    size: int
    last_modified: str | None = None


class NetworkVolumeClient:
    """Async client for RunPod network volume REST CRUD + S3 file access.

    REST operations target ``https://rest.runpod.io/v1`` (configurable via
    ``rest_base_url``). S3 operations target the datacenter-specific endpoint
    ``https://s3api-<dc>.runpod.io`` using per-volume bucket semantics
    (bucket = network volume ID).

    Credentials:
        * REST: ``RUNPOD_API_KEY`` env var or ``api_key`` constructor arg.
        * S3: ``RUNPOD_S3_ACCESS_KEY`` / ``RUNPOD_S3_SECRET_KEY`` env vars,
          falling back to ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``,
          or explicit ``s3_access_key`` / ``s3_secret_key`` args.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        rest_base_url: str = "https://rest.runpod.io/v1",
        s3_access_key: str | None = None,
        s3_secret_key: str | None = None,
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("RUNPOD_API_KEY", "")
        self._rest_base_url = rest_base_url.rstrip("/")
        self._s3_access_key = (
            s3_access_key
            or os.environ.get("RUNPOD_S3_ACCESS_KEY", "")
            or os.environ.get("AWS_ACCESS_KEY_ID", "")
        )
        self._s3_secret_key = (
            s3_secret_key
            or os.environ.get("RUNPOD_S3_SECRET_KEY", "")
            or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        )
        self._timeout_s = timeout_s
        self._transport = transport
        self._rest_client = httpx.AsyncClient(
            base_url=self._rest_base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {self._api_key}"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._rest_client.aclose()

    async def _rest_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        response = await self._rest_client.request(
            method,
            path,
            json=json_body,
        )
        if response.status_code == 204:
            return {}
        if response.status_code >= 400:
            raise RunPodRestError(method, path, response.status_code, response.text)
        if not response.content:
            return {}
        return response.json()

    async def create(self, name: str, size_gb: int, dc: str) -> NetworkVolume:
        """Create a new network volume and return its representation."""
        payload = {"name": name, "size": size_gb, "dataCenterId": dc}
        data = await self._rest_request("POST", "/networkvolumes", json_body=payload)
        if not isinstance(data, dict):
            raise RunPodError(
                f"create network volume returned unexpected type: {type(data).__name__}"
            )
        return NetworkVolume.model_validate(data)

    async def get(self, volume_id: str) -> NetworkVolume:
        """Fetch a single network volume by ID."""
        data = await self._rest_request("GET", f"/networkvolumes/{volume_id}")
        if not isinstance(data, dict):
            raise RunPodError(f"get network volume returned unexpected type: {type(data).__name__}")
        return NetworkVolume.model_validate(data)

    async def list(self) -> builtins.list[NetworkVolume]:
        """Return all network volumes owned by the account."""
        data = await self._rest_request("GET", "/networkvolumes")
        if not isinstance(data, list):
            raise RunPodError(
                f"list network volumes returned unexpected type: {type(data).__name__}"
            )
        return [NetworkVolume.model_validate(item) for item in data]

    async def update(self, volume_id: str, size_gb: int) -> NetworkVolume:
        """Resize a network volume (size must be larger than current)."""
        payload = {"size": size_gb}
        data = await self._rest_request("PATCH", f"/networkvolumes/{volume_id}", json_body=payload)
        if not isinstance(data, dict):
            raise RunPodError(
                f"update network volume returned unexpected type: {type(data).__name__}"
            )
        return NetworkVolume.model_validate(data)

    async def delete(self, volume_id: str) -> None:
        """Delete a network volume. Idempotent: silent on 404."""
        try:
            await self._rest_request("DELETE", f"/networkvolumes/{volume_id}")
        except RunPodRestError as exc:
            if exc.status_code == 404:
                log.info(
                    "network volume %s already deleted or never existed",
                    volume_id,
                )
                return
            raise

    def _s3_endpoint(self, dc: str) -> str:
        return f"https://s3api-{_s3_data_center_id(dc)}.runpod.io"

    def _s3_client(self, dc: str) -> Any:
        data_center_id = _s3_data_center_id(dc)
        try:
            import boto3
            from botocore.config import Config
        except ModuleNotFoundError as exc:
            raise RunPodError("boto3 is required for RunPod network volume S3 access") from exc

        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            region_name=data_center_id,
        )
        return boto3.client(
            "s3",
            endpoint_url=f"https://s3api-{data_center_id}.runpod.io",
            aws_access_key_id=self._s3_access_key,
            aws_secret_access_key=self._s3_secret_key,
            region_name=data_center_id,
            config=config,
        )

    async def list_objects(
        self,
        volume_id: str,
        dc: str,
        *,
        prefix: str = "",
    ) -> builtins.list[S3Object]:
        """List objects inside a network volume via S3 ListObjectsV2."""

        def _list() -> builtins.list[S3Object]:
            client = self._s3_client(dc)
            paginator = client.get_paginator("list_objects_v2")
            objects: builtins.list[S3Object] = []
            for page in paginator.paginate(Bucket=volume_id, Prefix=prefix):
                for obj in page.get("Contents", []) or []:
                    objects.append(
                        S3Object(
                            key=obj["Key"],
                            size=obj["Size"],
                            last_modified=obj.get("LastModified", ""),
                        )
                    )
            return objects

        return await asyncio.to_thread(_list)

    async def put_object(
        self,
        volume_id: str,
        dc: str,
        key: str,
        body: bytes,
    ) -> None:
        """Upload an object to a network volume via S3 PutObject."""

        def _put() -> None:
            client = self._s3_client(dc)
            client.put_object(Bucket=volume_id, Key=key, Body=body)

        await asyncio.to_thread(_put)

    async def get_object(
        self,
        volume_id: str,
        dc: str,
        key: str,
    ) -> bytes:
        """Download an object from a network volume via S3 GetObject."""

        def _get() -> bytes:
            client = self._s3_client(dc)
            response = client.get_object(Bucket=volume_id, Key=key)
            return cast(bytes, response["Body"].read())

        return await asyncio.to_thread(_get)

    async def delete_object(
        self,
        volume_id: str,
        dc: str,
        key: str,
    ) -> None:
        """Remove an object from a network volume via S3 DeleteObject."""

        def _delete() -> None:
            client = self._s3_client(dc)
            client.delete_object(Bucket=volume_id, Key=key)

        await asyncio.to_thread(_delete)


__all__ = [
    "POD_MOUNT_PATH",
    "POD_PROVIDER_TYPES",
    "POD_VOLUME_MOUNT_PATH",
    "PROVIDER_TYPE_MOUNT_PATHS",
    "PROVIDER_TYPE_VOLUME_MOUNT_PATHS",
    "SERVERLESS_MOUNT_PATH",
    "SERVERLESS_PROVIDER_TYPES",
    "SERVERLESS_VOLUME_MOUNT_PATH",
    "S3Object",
    "NetworkVolume",
    "NetworkVolumeClient",
    "mount_path_for_provider_type",
    "provider_type_volume_mount_path",
]
