"""RunPod Serverless Load-Balancer embedding client.

Wraps the LB surface that routes HTTP directly to worker pods:

    https://{ENDPOINT_ID}.api.runpod.ai/{CUSTOM_PATH}

The BGE-M3 worker exposes ``/embed`` on the LB endpoint.

Supports "Pitwall mode" via the ``PITWALL_EMBEDDING_VIA_PITWALL`` feature flag.
When enabled, embedding requests are routed through the Pitwall ``/v1/inference``
endpoint instead of going directly to the RunPod load balancer.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from pydantic import BaseModel

from pitwall.config import load_settings_from_env

MAX_REQUEST_BODY_BYTES = 30_000_000
_DEFAULT_RETRY_ATTEMPTS = 4
_DEFAULT_RETRY_BACKOFF_S = 2.0


class EmbeddingResponse(BaseModel):
    """Structured response from a BGE-M3 embedding call."""

    dense: list[list[float]] | None = None
    sparse: list[dict[int, float]] | None = None
    colbert: list[list[list[float]]] | None = None
    raw: dict[str, Any]


class ServerlessLBClient:
    """Async httpx wrapper for RunPod load-balancing Serverless BGE-M3 endpoints.

    When ``pitwall_embedding_via_pitwall`` is enabled (via the ``PITWALL_EMBEDDING_VIA_PITWALL``
    environment variable), embedding requests are routed through the Pitwall
    ``/v1/inference`` endpoint instead of directly to the RunPod load balancer.
    """

    def __init__(
        self,
        *,
        lb_base_url: str,
        api_key: str | None = None,
        timeout_s: float = 330.0,
        retry_attempts: int = _DEFAULT_RETRY_ATTEMPTS,
        retry_backoff_s: float = _DEFAULT_RETRY_BACKOFF_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if retry_attempts < 1:
            raise ValueError(f"retry_attempts must be >= 1, got {retry_attempts}")
        if retry_backoff_s < 0:
            raise ValueError(f"retry_backoff_s must be >= 0, got {retry_backoff_s}")
        normalized_url = lb_base_url.rstrip("/")
        if normalized_url.endswith("/embed"):
            normalized_url = normalized_url[: -len("/embed")]
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=normalized_url,
            timeout=timeout_s,
            headers=headers,
            transport=transport,
        )
        self._retry_attempts = retry_attempts
        self._retry_backoff_s = retry_backoff_s

    async def embed(
        self,
        texts: list[str],
        *,
        return_dense: bool = True,
        return_sparse: bool = True,
        return_colbert: bool = False,
    ) -> dict[str, Any]:
        settings = load_settings_from_env()
        if settings.pitwall_embedding_via_pitwall and settings.pitwall_base_url:
            return await self._embed_via_pitwall(
                texts,
                return_dense=return_dense,
                return_sparse=return_sparse,
                return_colbert=return_colbert,
            )
        return await self._embed_direct(
            texts,
            return_dense=return_dense,
            return_sparse=return_sparse,
            return_colbert=return_colbert,
        )

    async def _embed_via_pitwall(
        self,
        texts: list[str],
        *,
        return_dense: bool = True,
        return_sparse: bool = True,
        return_colbert: bool = False,
    ) -> dict[str, Any]:
        settings = load_settings_from_env()
        payload = {
            "capability": "embedding.bge-m3",
            "texts": texts,
            "return_dense": return_dense,
            "return_sparse": return_sparse,
            "return_colbert": return_colbert,
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) >= MAX_REQUEST_BODY_BYTES:
            raise ValueError(
                "Embedding request payload exceeds the RunPod load-balancer 30 MB limit; "
                "chunk smaller before embedding."
            )

        pitwall_url = settings.pitwall_base_url.rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._client.headers.get("Authorization"):
            headers["Authorization"] = self._client.headers["Authorization"]

        last_exc: Exception | None = None
        for attempt in range(self._retry_attempts):
            try:
                response = await self._client.post(
                    f"{pitwall_url}/v1/inference",
                    content=body,
                    headers=headers,
                )
                if response.status_code in (502, 503, 504):
                    response.raise_for_status()
                response.raise_for_status()
                data = response.json()
                result = data.get("result", {})
                return {
                    "dense": result.get("dense") if return_dense else None,
                    "sparse": result.get("sparse") if return_sparse else None,
                    "colbert": result.get("colbert") if return_colbert else None,
                    "raw": result,
                }
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == self._retry_attempts - 1:
                    break
                await asyncio.sleep(self._retry_backoff_s * (2**attempt))
        assert last_exc is not None
        raise last_exc

    async def _embed_direct(
        self,
        texts: list[str],
        *,
        return_dense: bool = True,
        return_sparse: bool = True,
        return_colbert: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "texts": texts,
            "return_dense": return_dense,
            "return_sparse": return_sparse,
            "return_colbert": return_colbert,
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) >= MAX_REQUEST_BODY_BYTES:
            raise ValueError(
                "Embedding request payload exceeds the RunPod load-balancer 30 MB limit; "
                "chunk smaller before embedding."
            )

        last_exc: Exception | None = None
        for attempt in range(self._retry_attempts):
            try:
                response = await self._client.post(
                    "/embed",
                    content=body,
                    headers={"Content-Type": "application/json"},
                )
                if response.status_code in (502, 503, 504):
                    response.raise_for_status()
                response.raise_for_status()
                data = response.json()
                return {
                    "dense": data.get("dense") if return_dense else None,
                    "sparse": data.get("sparse") if return_sparse else None,
                    "colbert": data.get("colbert") if return_colbert else None,
                    "raw": data,
                }
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == self._retry_attempts - 1:
                    break
                await asyncio.sleep(self._retry_backoff_s * (2**attempt))
        assert last_exc is not None
        raise last_exc

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = [
    "EmbeddingResponse",
    "MAX_REQUEST_BODY_BYTES",
    "ServerlessLBClient",
]
