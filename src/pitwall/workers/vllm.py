"""vLLM forwarder — async httpx wrapper for the vLLM OpenAI-compatible API.

This module provides a minimal forwarder client that routes inference requests
to a vLLM OpenAI-compatible endpoint. It is a library helper only; Pitwall does
not ship or support a GPU worker image in the public alpha (ADR 0002).

Unlike :class:`pitwall.runpod_client.serverless.ServerlessClient`, this
forwarder does not speak RunPod's serverless control plane — it forwards
requests directly to the locally-hosted vLLM process over HTTP.
"""

from __future__ import annotations

from typing import Any

import httpx


class VLLMForwarder:
    """Async client that forwards requests to a local vLLM OpenAI-compatible server."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: int = 300,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Initialize the forwarder.

        Args:
            base_url: Base URL of the vLLM OpenAI-compatible server
                      (e.g. ``http://127.0.0.1:8000``).
            timeout_s: Default request timeout in seconds.
            transport: Optional async transport for testing.
        """
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            headers={
                "Content-Type": "application/json",
            },
            transport=transport,
        )

    async def chat_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int,
        temperature: float = 0.0,
        stream: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Forward a chat-completion request to the vLLM server.

        Args:
            messages: List of OpenAI-style message dicts.
            model: Model name to request.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.
            stream: Whether to request a streaming response.
            extra: Additional fields to include in the request body.

        Returns:
            The raw httpx Response from the vLLM server.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if extra:
            payload.update(extra)

        return await self._client.post("/v1/chat/completions", json=payload)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()


__all__ = ["VLLMForwarder"]
